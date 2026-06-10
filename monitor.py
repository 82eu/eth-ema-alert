#!/usr/bin/env python3
"""ETH EMA 预警系统 - 核心监控模块（多数据源，兼容 Render 环境）"""
import json
import logging
import os
import time
import threading
import ssl
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.yaml')
HISTORY_FILE = os.path.join(BASE_DIR, 'alert_history.json')

TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h']
TF_LABELS = {'5m': '5分钟', '15m': '15分钟', '30m': '30分钟', '1h': '1小时', '4h': '4小时'}

# 不同 API 对周期字段的映射
GATE_TF = {'5m': '5m', '15m': '15m', '30m': '30m', '1h': '1h', '4h': '4h'}
BINANCE_TF = {'5m': '5m', '15m': '15m', '30m': '30m', '1h': '1h', '4h': '4h'}
BINANCE_FUTURE_TF = {'5m': '5m', '15m': '15m', '30m': '30m', '1h': '1h', '4h': '4h'}
KUCOIN_TF = {'5m': '5min', '15m': '15min', '30m': '30min', '1h': '1hour', '4h': '4hour'}

_state_cache = {}
_state_lock = threading.Lock()
_last_update_time = 0
_zone_tracker = {}
_last_source = {}
# 定时价格推送：记录上次推送的时间（UTC 小时分段）
_last_price_push_hour = -1  # -1 表示从未推送

# 数据源健康状态：{source_name: {'status': 'ok'|'fail', 'last_check': ts, 'message': str}}
_source_health = {}

# ==================== 数据源列表 ====================
# 按顺序尝试，第一个成功的就用。
# 每个条目返回 [[open, high, low, close], ...]，时间升序（旧→新）
_DATA_SOURCES = []


def _register_source(name, func):
    _DATA_SOURCES.append((name, func))


def _validate_klines(klines):
    """简单校验：最后一根 K线与前 10 根均值偏差超过 30% 视为数据异常。返回 True/False。"""
    if not isinstance(klines, list) or len(klines) < 15:
        return False
    last_close = float(klines[-1][3])
    recent = [float(k[3]) for k in klines[-12:-1]]
    if not recent:
        return False
    avg_recent = sum(recent) / len(recent)
    if avg_recent <= 0:
        return False
    if abs(last_close - avg_recent) / avg_recent > 0.3:
        return False
    # 二次校验：最后一根 K线是否为合理正数
    return 10 < last_close < 100000


def _source_gateio(tf, limit):
    """Gate.io 现货 K线（首选）"""
    url = 'https://api.gateio.ws/api/v4/spot/candlesticks'
    params = {'currency_pair': 'ETH_USDT', 'interval': GATE_TF[tf], 'limit': str(limit)}
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=12,
                                proxies={'http': None, 'https': None})
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) >= 10:
                    klines = [[float(k[5]), float(k[3]), float(k[4]), float(k[2])]
                              for k in data if len(k) >= 6]
                    if _validate_klines(klines):
                        return klines
        except Exception as e:
            last_err = e
        time.sleep(0.5 + attempt)
    if last_err:
        logger.warning(f"[{tf}] Gate.io 失败: {last_err}")
    return None


def _source_binance_spot(tf, limit):
    """Binance 现货 K线（全球可用）"""
    url = 'https://api.binance.com/api/v3/klines'
    params = {'symbol': 'ETHUSDT', 'interval': BINANCE_TF[tf], 'limit': str(limit)}
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10,
                                proxies={'http': None, 'https': None})
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) >= 10:
                    klines = [[float(k[1]), float(k[2]), float(k[3]), float(k[4])]
                              for k in data if len(k) >= 6]
                    if _validate_klines(klines):
                        return klines
        except Exception as e:
            last_err = e
        time.sleep(0.5 + attempt)
    logger.warning(f"[{tf}] Binance 失败: {last_err}")
    return None


def _source_kucoin(tf, limit):
    """KuCoin K线（备用）"""
    url = 'https://api.kucoin.com/api/v1/market/candles'
    params = {'symbol': 'ETH-USDT', 'type': KUCOIN_TF[tf]}
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10,
                            proxies={'http': None, 'https': None})
        if resp.status_code == 200:
            wrapper = resp.json()
            data = wrapper.get('data', []) if isinstance(wrapper, dict) else []
            if isinstance(data, list) and len(data) >= 10:
                # KuCoin: [time, open, close, high, low, volume, turnover]，**时间降序**
                klines = [[float(k[1]), float(k[3]), float(k[4]), float(k[2])] for k in data if len(k) >= 6]
                klines.reverse()
                klines = klines[:limit]
                if _validate_klines(klines):
                    return klines
    except Exception as e:
        logger.warning(f"[{tf}] KuCoin 失败: {e}")
    return None


def _source_coingecko(tf, limit):
    """CoinGecko（备用，1h/4h 周期更稳定）"""
    url = 'https://api.coingecko.com/api/v3/coins/ethereum/ohlc'
    days = '365' if tf in ['1h', '4h'] else '1'
    params = {'vs_currency': 'usd', 'days': days}
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10,
                            proxies={'http': None, 'https': None})
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) >= 10:
                klines = [[float(k[1]), float(k[2]), float(k[3]), float(k[4])]
                          for k in data if len(k) >= 5]
                if _validate_klines(klines):
                    return klines
    except Exception as e:
        logger.warning(f"[{tf}] CoinGecko 失败: {e}")
    return None


# 注册数据源（只保留真实交易所）
_register_source('gateio', _source_gateio)              # 第一优先
_register_source('binance_spot', _source_binance_spot)  # 次选
_register_source('kucoin', _source_kucoin)              # 备用
_register_source('coingecko', _source_coingecko)        # 备用


# ============ 配置读写（支持环境变量覆盖，Render 部署推荐） ============
def load_config():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
        cfg = yaml.safe_load(content) or {}
    except Exception:
        cfg = {}

    if 'ema_alert' not in cfg or not isinstance(cfg['ema_alert'], dict):
        cfg['ema_alert'] = {}
    cfg['ema_alert'].setdefault('ema_short', 180)
    cfg['ema_alert'].setdefault('ema_long', 250)
    cfg['ema_alert'].setdefault('enabled_timeframes', ['30m', '1h'])
    cfg['ema_alert'].setdefault('timeframes', TIMEFRAMES)

    if 'alert' not in cfg or not isinstance(cfg['alert'], dict):
        cfg['alert'] = {}
    cfg['alert'].setdefault('cooldown_seconds', 600)
    cfg['alert'].setdefault('check_interval', 30)

    if 'email' not in cfg or not isinstance(cfg['email'], dict):
        cfg['email'] = {}
    cfg['email'].setdefault('smtp_server', 'smtp.qq.com')
    cfg['email'].setdefault('smtp_port', 465)
    cfg['email'].setdefault('use_ssl', True)
    cfg['email'].setdefault('from_email', '')
    cfg['email'].setdefault('password', '')
    cfg['email'].setdefault('username', '')
    cfg['email'].setdefault('to_email', '')

    # 环境变量覆盖（Render 部署用，优先级最高）
    env_from = os.environ.get('ALERT_FROM_EMAIL', '').strip()
    env_to = os.environ.get('ALERT_TO_EMAIL', '').strip()
    env_pwd = os.environ.get('ALERT_EMAIL_PASSWORD', '').strip()
    env_smtp = os.environ.get('ALERT_SMTP_SERVER', '').strip()
    env_port = os.environ.get('ALERT_SMTP_PORT', '').strip()
    if env_from: cfg['email']['from_email'] = env_from
    if env_to: cfg['email']['to_email'] = env_to
    if env_pwd: cfg['email']['password'] = env_pwd
    if env_smtp: cfg['email']['smtp_server'] = env_smtp
    if env_port:
        try: cfg['email']['smtp_port'] = int(env_port)
        except: pass
    if env_from and not cfg['email'].get('username'):
        cfg['email']['username'] = env_from

    # 飞书 Webhook 配置（优先级最高的推送方式）
    if 'feishu' not in cfg or not isinstance(cfg['feishu'], dict):
        cfg['feishu'] = {}
    cfg['feishu'].setdefault('webhook', '')
    env_feishu = os.environ.get('FEISHU_WEBHOOK', '').strip()
    if env_feishu:
        cfg['feishu']['webhook'] = env_feishu
    # 是否同时发邮件（默认关）
    cfg['feishu'].setdefault('also_send_email', False)
    env_also = os.environ.get('FEISHU_ALSO_EMAIL', '').strip().lower()
    if env_also in ['1', 'true', 'yes']:
        cfg['feishu']['also_send_email'] = True

    # 价格区间预警配置
    if 'price_ranges' not in cfg or not isinstance(cfg['price_ranges'], list):
        cfg['price_ranges'] = []
    # 每个区间格式: {name, low, high, enabled}

    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False)
    except Exception as e:
        logger.error(f"保存配置失败: {e}")


# ============ 历史记录 ============
def load_history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def save_history(history):
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def add_alert_record(tf, price, ema_s, ema_l, signal, position, alert_type='ema_zone', note=''):
    """写入一条预警记录。alert_type: 'ema_zone' / 'price_range'"""
    history = load_history()
    record = {
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'timestamp': time.time(),
        'timeframe': tf,
        'price': round(price, 2),
        'ema_short': round(ema_s, 2),
        'ema_long': round(ema_l, 2),
        'signal': signal,
        'position': position,
        'alert_type': alert_type,
        'note': note,
    }
    history.append(record)
    if len(history) > 200:
        history = history[-200:]
    save_history(history)


def delete_alert_by_timestamp(ts):
    """删除指定 timestamp 的预警记录。返回 True/False"""
    try:
        history = load_history()
        new_history = [h for h in history if abs(float(h.get('timestamp', 0)) - float(ts)) > 0.5]
        if len(new_history) == len(history):
            return False
        save_history(new_history)
        return True
    except Exception:
        return False


def clear_all_alerts():
    """清空所有预警记录"""
    try:
        save_history([])
        return True
    except Exception:
        return False


def get_recent_alerts(limit=10):
    """返回最近的预警记录（含完整字段）"""
    try:
        history = load_history()
        result = []
        for h in reversed(history[-limit:]):
            result.append({
                'time': h.get('time', ''),
                'timestamp': float(h.get('timestamp', 0)),
                'timeframe': h.get('timeframe', ''),
                'price': float(h.get('price', 0)),
                'ema_short': float(h.get('ema_short', 0)),
                'ema_long': float(h.get('ema_long', 0)),
                'signal': str(h.get('signal', '')),
                'position': str(h.get('position', '')),
                'alert_type': str(h.get('alert_type', 'ema_zone')),
                'note': str(h.get('note', '')),
            })
        return result
    except Exception:
        return []


# ============ K线数据获取（多数据源） ============
def fetch_klines(tf, limit=1000):
    """依次尝试多个数据源，返回第一个成功的 K线数据 [[open, high, low, close], ...]"""
    for name, source_fn in _DATA_SOURCES:
        try:
            klines = source_fn(tf, limit)
            if klines and isinstance(klines, list) and len(klines) >= 20:
                if name == 'synthetic':
                    logger.warning(f"[{tf}] ⚠️  使用合成数据（所有在线数据源不可用）")
                else:
                    logger.info(f"[{tf}] 使用数据源: {name}")
                _last_source[tf] = name
                _source_health[name] = {
                    'status': 'ok',
                    'last_check': time.time(),
                    'message': '正常',
                }
                return klines
            else:
                _source_health[name] = {
                    'status': 'fail',
                    'last_check': time.time(),
                    'message': '返回数据不足',
                }
        except Exception as e:
            logger.warning(f"[{tf}] 数据源 {name} 失败: {e}")
            _source_health[name] = {
                'status': 'fail',
                'last_check': time.time(),
                'message': str(e)[:200],
            }
            continue
    logger.error(f"[{tf}] ⚠️  所有数据源都失败")
    _source_health['_overall'] = {
        'status': 'fail',
        'last_check': time.time(),
        'message': '所有在线数据源失败',
    }
    return []


# ============ EMA 计算 ============
def calc_ema(closes, period):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for p in closes[period:]:
        ema = (p - ema) * k + ema
    return ema


# ============ 信号分析 ============
def analyze(tf, klines, ema_short, ema_long):
    if not klines or len(klines) < ema_long + 10:
        return None

    closes = [k[3] for k in klines]
    price = closes[-1]
    es = calc_ema(closes, ema_short)
    el = calc_ema(closes, ema_long)
    if es is None or el is None:
        return None

    ema_high = max(es, el)
    ema_low = min(es, el)

    if price > ema_high:
        position = 'above'
        pos_text = '在双EMA上方'
    elif price < ema_low:
        position = 'below'
        pos_text = '在双EMA下方'
    else:
        position = 'between'
        pos_text = '在EMA区间内'

    if es > el:
        arrangement = 'bull'
        arr_text = '多头排列'
    else:
        arrangement = 'bear'
        arr_text = '空头排列'

    if position == 'between':
        if arrangement == 'bull':
            signal = '开多'
            signal_text = '🟢 开多'
        else:
            signal = '开空'
            signal_text = '🔴 开空'
    else:
        signal = '观望'
        signal_text = f'⚪ 观望({arr_text})'

    return {
        'timeframe': tf,
        'label': TF_LABELS.get(tf, tf),
        'price': price,
        'ema_short': es,
        'ema_long': el,
        'ema_high': ema_high,
        'ema_low': ema_low,
        'position': position,
        'position_text': pos_text,
        'arrangement': arrangement,
        'arrangement_text': arr_text,
        'signal': signal,
        'signal_text': signal_text,
        'in_zone': (position == 'between'),
        'update_time': datetime.now().strftime('%H:%M:%S'),
        'data_count': len(klines),
        'data_source': _last_source.get(tf, 'unknown'),
    }


# ============ 邮件发送 ============
def send_email(subject, body_html, cfg):
    email_cfg = cfg.get('email', {})
    if not email_cfg.get('from_email') or not email_cfg.get('password') or not email_cfg.get('to_email'):
        return False
    last_err = None
    for attempt in range(2):
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = email_cfg['from_email']
            msg["To"] = email_cfg['to_email']
            msg.attach(MIMEText(body_html, "html", "utf-8"))

            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                email_cfg.get('smtp_server', 'smtp.qq.com'),
                int(email_cfg.get('smtp_port', 465)),
                context=context, timeout=30
            ) as server:
                server.login(email_cfg.get('username', email_cfg['from_email']), email_cfg['password'])
                server.sendmail(email_cfg['from_email'], [email_cfg['to_email']], msg.as_string())
            logger.info(f"✅ 邮件已发送: {subject}")
            return True
        except Exception as e:
            last_err = e
            time.sleep(2)
    logger.warning(f"⚠️ 邮件发送失败: {last_err}")
    return False


# ============ 飞书推送（推荐）============
def send_feishu(title, content_text, cfg):
    """通过飞书机器人 Webhook 推送消息。text 格式即可，支持换行。"""
    webhook = ''
    if isinstance(cfg, dict):
        fc = cfg.get('feishu', {}) if isinstance(cfg.get('feishu'), dict) else {}
        webhook = fc.get('webhook', '')
    if not webhook:
        # 直接从环境变量读（防止传进来的 cfg 不对）
        webhook = os.environ.get('FEISHU_WEBHOOK', '').strip()
    if not webhook:
        logger.warning("⚠️ 飞书 Webhook 未配置，跳过推送")
        return False
    try:
        text = f"{title}\n\n{content_text}"
        payload = {'msg_type': 'text', 'content': {'text': text}}
        resp = requests.post(webhook, json=payload, timeout=15)
        if resp.status_code == 200:
            j = resp.json() if resp.content else {}
            if j.get('code') == 0 or j.get('StatusCode') == 0 or j.get('StatusCode') is None and j.get('code') is None:
                # 飞书返回 OK（或 200 空响应）
                logger.info(f"✅ 飞书推送成功: {title}")
                return True
            logger.warning(f"⚠️ 飞书返回非 OK: {resp.text[:200]}")
            return False
        logger.warning(f"⚠️ 飞书 HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"⚠️ 飞书推送失败: {e}")
        return False


# ============ 统一报警发送接口（先飞书，失败再邮件）============
def send_alert(subject, body_text, body_html, cfg):
    """统一报警接口：优先飞书，可选择是否同时发邮件。"""
    ok = False
    ok = send_feishu(subject, body_text, cfg)
    if cfg.get('feishu', {}).get('also_send_email', False) or not ok:
        # 配置了同时发邮件 或 飞书失败 时尝试邮件
        if body_html:
            send_email(subject, body_html, cfg)
    return ok


# ============ 数据更新 ============
def update_all_data():
    """更新所有周期的数据。失败的周期保留上次缓存。"""
    global _last_update_time
    cfg = load_config()
    ema_s_p = cfg['ema_alert']['ema_short']
    ema_l_p = cfg['ema_alert']['ema_long']
    cooldown = cfg['alert'].get('cooldown_seconds', 600)
    enabled_tfs = cfg['ema_alert'].get('enabled_timeframes', [])
    price_ranges = cfg.get('price_ranges', []) or []

    # 初始化缓存：确保每个周期都有默认占位，避免前端看到 $0.00
    with _state_lock:
        for tf in TIMEFRAMES:
            if tf not in _state_cache or _state_cache[tf] is None:
                _state_cache[tf] = {
                    'timeframe': tf, 'label': TF_LABELS.get(tf, tf),
                    'price': 0, 'ema_short': 0, 'ema_long': 0,
                    'ema_high': 0, 'ema_low': 0,
                    'position': '', 'position_text': '加载中...',
                    'arrangement': '', 'arrangement_text': '加载中',
                    'signal': '', 'signal_text': '加载中',
                    'in_zone': False, 'update_time': '加载中',
                    'data_count': 0, 'data_source': 'loading',
                    '_stale': True,
                }

    has_any_data = False
    for tf in TIMEFRAMES:
        try:
            klines = fetch_klines(tf, max(ema_l_p * 4, 500))
            if not klines:
                logger.warning(f"[{tf}] ⚠️  本轮获取失败，保留上次缓存")
                # 标记上次缓存为 stale（但不清空）
                with _state_lock:
                    if _state_cache.get(tf):
                        _state_cache[tf]['_stale'] = True
                        if '加载中' in str(_state_cache[tf].get('update_time', '')):
                            _state_cache[tf]['update_time'] = '数据暂不可用'
                continue
            result = analyze(tf, klines, ema_s_p, ema_l_p)
            if result is None:
                with _state_lock:
                    if _state_cache.get(tf):
                        _state_cache[tf]['_stale'] = True
                continue

            result['_stale'] = False
            has_any_data = True

            with _state_lock:
                _state_cache[tf] = result

            if tf in enabled_tfs:
                tracker = _zone_tracker.setdefault(
                    tf, {'in_zone_prev': False, 'left_time': 0}
                )
                in_zone = result['in_zone']
                now = time.time()

                if in_zone:
                    if not tracker['in_zone_prev']:
                        last_map = _recover_last_alert_time()
                        last_alert_time = last_map.get(tf, 0)
                        if now - last_alert_time > cooldown:
                            subject = f"[ETH EMA 预警] {TF_LABELS.get(tf, tf)} · {result['signal_text']} · ${result['price']:.2f}"
                            body = f"""
                            <html><body style="font-family: sans-serif; max-width: 500px; margin: 0 auto; padding: 20px;">
                                <h2 style="color:#667eea;">{subject}</h2>
                                <div style="background:#f8fafc; padding: 15px; border-radius: 8px; margin-top: 10px;">
                                    <div style="font-size: 28px; font-weight: bold; text-align: center;">${result['price']:.2f}</div>
                                    <div style="margin-top: 15px;">
                                        <div>EMA{ema_s_p}: <b>${result['ema_short']:.2f}</b></div>
                                        <div>EMA{ema_l_p}: <b>${result['ema_long']:.2f}</b></div>
                                        <div style="margin-top: 10px;">{result['arrangement_text']} · {result['position_text']}</div>
                                    </div>
                                </div>
                                <div style="margin-top: 15px; color: #64748b; font-size: 12px; text-align: center;">
                                    触发时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 周期: {TF_LABELS.get(tf, tf)}
                                </div>
                            </body></html>
                            """
                            body_text = f"EMA{ema_s_p} 与 EMA{ema_l_p} 金叉/死叉\n当前价格: ${result['price']:.2f}\nEMA{ema_s_p}: ${result['ema_short']:.2f}\nEMA{ema_l_p}: ${result['ema_long']:.2f}\n排列状态: {result['arrangement_text']} · {result['position_text']}\n触发时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 周期: {TF_LABELS.get(tf, tf)}"
                            if send_alert(subject, body_text, body, cfg):
                                add_alert_record(
                                    tf, result['price'], result['ema_short'],
                                    result['ema_long'], result['signal'], result['position']
                                )
                                logger.info(f"[{tf}] ✅ 触发预警 - 价格=${result['price']:.2f}")
                    tracker['in_zone_prev'] = True
                    tracker['left_time'] = 0
                else:
                    if tracker['in_zone_prev']:
                        tracker['left_time'] = now
                        tracker['in_zone_prev'] = False

        except Exception as e:
            logger.warning(f"[{tf}] 检查出错: {e}")

        time.sleep(0.3)  # 轻量间隔，避免 API 限流

    # ===== 价格区间预警（使用 5m 周期的价格作为参考价格）=====
    if price_ranges:
        ref_price = None
        ref_state = _state_cache.get('5m') or _state_cache.get('15m')
        if ref_state and ref_state.get('price'):
            ref_price = float(ref_state['price'])

        if ref_price and ref_price > 0:
            for idx, pr in enumerate(price_ranges):
                try:
                    if not pr.get('enabled', False):
                        continue
                    low = float(pr.get('low', 0))
                    high = float(pr.get('high', 0))
                    if low <= 0 or high <= 0 or low >= high:
                        continue
                    # 跟踪器
                    pr_track = _zone_tracker.setdefault(f'price_range_{idx}', {'in_zone_prev': False, 'left_time': 0})
                    in_range = low <= ref_price <= high
                    now = time.time()
                    if in_range and not pr_track['in_zone_prev']:
                        last_map = _recover_last_alert_time()
                        last_ts = last_map.get(f'price_range_{idx}', 0)
                        if now - last_ts > cooldown:
                            note = pr.get('name', f'区间{idx+1}')
                            subject = f"[ETH EMA 预警] 价格区间 · {note} · ${ref_price:.2f}"
                            body = f"""
                            <html><body style="font-family: sans-serif; max-width: 500px; margin: 0 auto; padding: 20px;">
                                <h2 style="color:#f97316;">{subject}</h2>
                                <div style="background:#f8fafc; padding: 15px; border-radius: 8px; margin-top: 10px;">
                                    <div style="font-size: 28px; font-weight: bold; text-align: center;">${ref_price:.2f}</div>
                                    <div style="margin-top: 15px; font-size: 16px;">
                                        <div>区间名称: <b>{note}</b></div>
                                        <div>上限: <b>${high:.2f}</b></div>
                                        <div>下限: <b>${low:.2f}</b></div>
                                    </div>
                                </div>
                                <div style="margin-top: 15px; color: #64748b; font-size: 12px; text-align: center;">
                                    触发时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
                                </div>
                            </body></html>
                            """
                            body_text = f"价格区间预警 - {note}\n当前价格: ${ref_price:.2f}\n区间上限: ${high:.2f}\n区间下限: ${low:.2f}\n触发时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                            if send_alert(subject, body_text, body, cfg):
                                add_alert_record(
                                    '5m', ref_price, low, high,
                                    '进入区间', 'between', alert_type='price_range', note=note
                                )
                                logger.info(f"[价格区间] ✅ {note} - 价格=${ref_price:.2f}，区间 [${low:.2f}, ${high:.2f}]")
                    pr_track['in_zone_prev'] = in_range
                    if not in_range:
                        pr_track['left_time'] = now
                except Exception as e:
                    logger.error(f"价格区间预警 {idx} 检查失败: {e}")

    if has_any_data:
        _last_update_time = time.time()
        latest = None
        for tf in ['5m', '15m', '30m', '1h', '4h']:
            if tf in _state_cache and _state_cache[tf]:
                latest = _state_cache[tf]
                break
        if latest:
            logger.info(f"✅ 数据更新完成 - ETH=${latest['price']:.2f}, 数据源={latest['data_source']}")

        # ===== 定时价格推送（每4小时一次）=====
        try:
            should_push, hour = _should_push_price_now()
            if should_push:
                logger.info(f"⏰ 到了 {hour:02d}:00 推送时间，正在推送价格到飞书...")
                push_ok = _push_price_to_feishu(cfg)
                if push_ok:
                    _last_price_push_hour = hour
                    logger.info(f"✅ {hour:02d}:00 价格推送已完成")
                else:
                    # 推送失败时，重置标记，让下次循环再尝试
                    logger.warning(f"⚠️  {hour:02d}:00 价格推送失败，稍后重试")
                    pass
        except Exception as e:
            logger.warning(f"定时价格推送出错: {e}")

        return True
    logger.warning("⚠️ 所有数据源均获取失败")
    return False


def _recover_last_alert_time():
    last_map = {}
    try:
        history = load_history()
        for r in reversed(history):
            tf = r.get('timeframe')
            if tf and tf not in last_map:
                last_map[tf] = r.get('timestamp', 0)
    except Exception:
        pass
    return last_map


# ============ 定时价格推送（每4小时一次）============
def _should_push_price_now():
    """判断当前是否到了推送时间。
    以服务器本地时间计算：
      - 从 08:00 开始，每 4 小时一次（08, 12, 16, 20, 00, 04）
      - 在这些整点的前 2 分钟内，且尚未推送过该小时段
    """
    now = datetime.now()
    hour = now.hour
    minute = now.minute
    # 目标小时：0, 4, 8, 12, 16, 20
    if hour % 4 != 0:
        return False, -1
    # 在整点的 0-10 分钟内触发（给数据更新留时间）
    if minute > 10:
        return False, -1
    global _last_price_push_hour
    if _last_price_push_hour == hour:
        return False, -1
    return True, hour


def _push_price_to_feishu(cfg):
    """把当前价格推送到飞书。"""
    price = None
    source_name = 'unknown'
    # 取最新可用的价格
    for tf in ['5m', '15m', '30m', '1h', '4h']:
        s = _state_cache.get(tf)
        if s and isinstance(s, dict) and s.get('price'):
            try:
                p = float(s.get('price', 0))
                if p > 0:
                    price = p
                    source_name = s.get('data_source', 'unknown')
                    break
            except Exception:
                continue

    if price is None:
        logger.warning("⚠️  定时推送：无有效价格，跳过")
        return False

    subject = f"ETH 价格播报 · ${price:.2f}"
    content = f"更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    ok = send_feishu(subject, content, cfg)
    if ok:
        logger.info(f"✅ 定时价格推送成功 - ${price:.2f}")
    else:
        logger.warning(f"⚠️  定时价格推送失败 - ${price:.2f}")
    return ok


# ============ 后台监控循环 ============
def run_monitor_loop():
    logger.info("=" * 50)
    logger.info("🚀 ETH EMA 预警系统启动 (后台线程)")
    logger.info("=" * 50)

    update_all_data()

    # 启动时发一条通知到飞书
    try:
        cfg = load_config()
        price = get_latest_price()
        if price:
            subject = f"ETH 预警系统已启动 · ${price:.2f}"
            content = f"更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n推送时间表: 08:00 / 12:00 / 16:00 / 20:00 / 00:00 / 04:00"
            send_feishu(subject, content, cfg)
    except Exception as e:
        logger.warning(f"启动通知失败: {e}")

    while True:
        try:
            time.sleep(30)
            update_all_data()
        except Exception as e:
            logger.error(f"主循环出错: {e}")
            time.sleep(30)


# ============ 对外接口 ============
def get_all_states():
    return dict(_state_cache)


def get_latest_price():
    for tf in ['5m', '15m', '30m', '1h', '4h']:
        if tf in _state_cache and _state_cache[tf]:
            return _state_cache[tf]['price']
    return None


def get_last_update_time():
    return _last_update_time


def get_source_health():
    """返回每个数据源最近一次健康状态"""
    result = {}
    for name, _ in _DATA_SOURCES:
        h = _source_health.get(name)
        if h:
            result[name] = h
        else:
            result[name] = {'status': 'unknown', 'last_check': 0, 'message': '未检测'}
    return result


def get_connection_status():
    """聚合连接状态：
    - 'online': 至少一个真实数据源正常工作
    - 'offline': 所有数据源都失败
    """
    for name, _ in _DATA_SOURCES:
        h = _source_health.get(name)
        if h and h['status'] == 'ok':
            return {'level': 'online', 'label': '✅ 在线', 'detail': '交易所连接正常，使用实时数据'}
    return {'level': 'offline', 'label': '🔴 离线', 'detail': '所有交易所API暂时不可用，正在自动重试...'}


def start_monitor_in_background():
    t = threading.Thread(target=run_monitor_loop, daemon=True)
    t.start()
    return t
