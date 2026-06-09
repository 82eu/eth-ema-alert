#!/usr/bin/env python3
"""ETH EMA 预警系统 - 核心监控模块"""
import asyncio
import aiohttp
import json
import logging
import os
import time
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.yaml')
HISTORY_FILE = os.path.join(BASE_DIR, 'alert_history.json')

# 支持的周期
TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h']
TF_LABELS = {'5m': '5分钟', '15m': '15分钟', '30m': '30分钟', '1h': '1小时', '4h': '4小时'}

# 全局状态（供 web 页面查询）
state_cache = {}
state_lock = asyncio.Lock()

# ============== 配置读写 ==============
def load_config():
    try:
        # 用 yaml 加载（保持兼容）
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
        # 简易 YAML 解析（避免依赖复杂）
        import yaml
        cfg = yaml.safe_load(content) or {}
    except Exception:
        cfg = {}

    # 确保有默认值
    if 'ema_alert' not in cfg or not isinstance(cfg['ema_alert'], dict):
        cfg['ema_alert'] = {}
    cfg['ema_alert'].setdefault('ema_short', 180)
    cfg['ema_alert'].setdefault('ema_long', 250)
    cfg['ema_alert'].setdefault('enabled_timeframes', ['30m', '1h'])  # 默认勾选30m和1h
    cfg['ema_alert'].setdefault('timeframes', TIMEFRAMES)

    if 'alert' not in cfg or not isinstance(cfg['alert'], dict):
        cfg['alert'] = {}
    cfg['alert'].setdefault('cooldown_seconds', 600)  # 10分钟冷却
    cfg['alert'].setdefault('check_interval', 60)

    if 'email' not in cfg or not isinstance(cfg['email'], dict):
        cfg['email'] = {}
    cfg['email'].setdefault('smtp_server', 'smtp.qq.com')
    cfg['email'].setdefault('smtp_port', 465)
    cfg['email'].setdefault('use_ssl', True)
    cfg['email'].setdefault('from_email', '')
    cfg['email'].setdefault('password', '')
    cfg['email'].setdefault('username', '')
    cfg['email'].setdefault('to_email', '')

    return cfg


def save_config(cfg):
    import yaml
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False)


# ============== 历史记录 ==============
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


def add_alert_record(tf, price, ema_s, ema_l, signal, position):
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
        'alert_type': 'ema_zone',
    }
    history.append(record)
    # 只保留最近100条
    if len(history) > 100:
        history = history[-100:]
    save_history(history)


# ============== K线 & EMA 计算 ==============
async def fetch_klines(tf, limit=1000):
    """从 Gate.io 获取 K线数据，返回 [[open, high, low, close], ...] 按时间正序（旧→新）"""
    url = 'https://api.gateio.ws/api/v4/spot/candlesticks'
    params = {'currency_pair': 'ETH_USDT', 'interval': tf, 'limit': str(limit)}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                if isinstance(data, list) and len(data) > 10 and isinstance(data[0], list):
                    # Gate.io 返回顺序：data[0]=最早(时间戳最小), data[-1]=最新(时间戳最大)
                    # EMA计算需要旧→新，所以直接用原顺序即可
                    # [timestamp, volume, open, high, low, close]
                    klines = [[float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in data]
                    return klines
    except Exception as e:
        logger.warning(f"[{tf}] 获取K线失败: {e}")
    return []


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for p in closes[period:]:
        ema = (p - ema) * k + ema
    return ema


def analyze(tf, klines, ema_short, ema_long):
    """分析一个周期的状态，返回 dict"""
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

    # 信号判断：只有真正在区间内（between）= 开多/开空；否则= 观望
    if position == 'between':
        if arrangement == 'bull':
            signal = '开多'
            signal_text = '🟢 开多'
        else:
            signal = '开空'
            signal_text = '🔴 开空'
    elif position == 'above':
        if arrangement == 'bull':
            signal = '观望(上方多头)'
            signal_text = '⚪ 观望(上方·多头排列)'
        else:
            signal = '观望(上方空头)'
            signal_text = '⚪ 观望(上方·空头排列)'
    else:  # below
        if arrangement == 'bear':
            signal = '观望(下方空头)'
            signal_text = '⚪ 观望(下方·空头排列)'
        else:
            signal = '观望(下方多头)'
            signal_text = '⚪ 观望(下方·多头排列)'

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
    }


# ============== 邮件 ==============
def send_email(subject, body_html, cfg):
    email_cfg = cfg.get('email', {})
    if not email_cfg.get('from_email') or not email_cfg.get('password') or not email_cfg.get('to_email'):
        logger.info("邮箱未配置，跳过发送邮件")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = email_cfg['from_email']
        msg["To"] = email_cfg['to_email']
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(email_cfg.get('smtp_server', 'smtp.qq.com'),
                              int(email_cfg.get('smtp_port', 465)),
                              context=context, timeout=30) as server:
            server.login(email_cfg.get('username', email_cfg['from_email']), email_cfg['password'])
            server.sendmail(email_cfg['from_email'], [email_cfg['to_email']], msg.as_string())
        logger.info(f"✅ 邮件已发送: {subject}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


# ============== 监控循环 ==============
# 追踪每个周期的区间进入/脱离状态（存在内存，但关键的 last_alert_time 会持久化到 alert_history 里）
zone_tracker = {}

# 从历史记录中恢复每个周期最后一次预警时间
def _recover_last_alert_time():
    """从 alert_history 中恢复每个周期最后一次预警的时间，用于 cooldown 判断"""
    last_map = {}
    try:
        history = load_history()
        # 倒着遍历，每个周期只取最后一条
        for r in reversed(history):
            tf = r.get('timeframe')
            if tf and tf not in last_map:
                last_map[tf] = r.get('timestamp', 0)
    except Exception:
        pass
    return last_map


async def check_one_tf(tf, cfg, only_update=False):
    """检查单个周期，更新 state_cache。only_update=True 时只更新数据不发预警。"""
    ema_s_p = cfg['ema_alert']['ema_short']
    ema_l_p = cfg['ema_alert']['ema_long']

    klines = await fetch_klines(tf, max(ema_l_p * 4, 1000))
    if not klines:
        logger.warning(f"[{tf}] K线获取失败")
        return None

    result = analyze(tf, klines, ema_s_p, ema_l_p)
    if result is None:
        return None

    # 更新缓存（供网页显示）
    async with state_lock:
        state_cache[tf] = result

    # 仅更新数据模式，跳过预警判断
    if only_update:
        return result

    # 判断是否需要发预警（只对已勾选的周期）
    enabled_tfs = cfg['ema_alert'].get('enabled_timeframes', [])
    if tf not in enabled_tfs:
        return result

    tracker = zone_tracker.setdefault(tf, {'in_zone_prev': False, 'left_time': 0})

    cooldown = cfg['alert'].get('cooldown_seconds', 1800)   # 默认30分钟冷却，避免反复骚扰
    now = time.time()
    in_zone = result['in_zone']

    if in_zone:
        if not tracker['in_zone_prev']:
            # 刚从"不在区间"变为"在区间内" → 检查cooldown后发预警
            last_map = _recover_last_alert_time()
            last_alert_time = last_map.get(tf, 0)
            if now - last_alert_time > cooldown:
                subject = f"🚨 [{tf}] ETH {result['signal_text']} 价格=${result['price']:.2f}"
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
                if send_email(subject, body, cfg):
                    # 成功发送邮件后再写入历史，防止脏数据
                    add_alert_record(tf, result['price'], result['ema_short'], result['ema_long'],
                                     result['signal'], result['position'])
                    logger.info(f"[{tf}] ✅ 触发预警 - 价格=${result['price']:.2f}, EMA{ema_s_p}=${result['ema_short']:.2f}, EMA{ema_l_p}=${result['ema_long']:.2f}")
        tracker['in_zone_prev'] = True
        tracker['left_time'] = 0
    else:
        # 不在区间内 → 记录离开时间（用于"真的脱离了"才允许下次预警）
        if tracker['in_zone_prev']:
            tracker['left_time'] = now
            tracker['in_zone_prev'] = False
        # 脱离区间超过 cooldown 的 80%，才允许下次重新触发（防止震荡骚扰）
        if tracker['left_time'] > 0 and (now - tracker['left_time']) < cooldown * 0.8:
            # 还在"刚脱离不久"，不计入"可重新预警"
            pass
        # 不主动重置 last_alert_time（由历史记录文件控制）

    return result


async def run_monitor():
    """主监控循环 - 在后台每60秒检查一次"""
    logger.info("=" * 50)
    logger.info("🚀 ETH EMA 预警系统启动")
    logger.info("=" * 50)

    # 数据获取循环（每10秒更新一次价格/EMA数据）
    data_fetch_loop = 0
    while True:
        try:
            cfg = load_config()
            data_fetch_loop += 1

            # 每10秒获取一次最新数据（更新页面显示用）
            for tf in TIMEFRAMES:
                try:
                    result = await check_one_tf(tf, cfg, only_update=True)
                    if result and data_fetch_loop % 6 == 1:  # 每60秒打印一次日志，避免刷屏
                        logger.info(f"[{tf}] 价格=${result['price']:.2f}, EMA{cfg['ema_alert']['ema_short']}=${result['ema_short']:.2f}, EMA{cfg['ema_alert']['ema_long']}=${result['ema_long']:.2f}, {result['signal_text']}")
                except Exception as e:
                    logger.error(f"[{tf}] 检查出错: {e}")
                await asyncio.sleep(0.5)

            await asyncio.sleep(8)
        except Exception as e:
            logger.error(f"主循环出错: {e}")
            await asyncio.sleep(30)


# ============== 工具函数：获取所有周期的状态（供 web 页面用）==============
def get_all_states():
    return dict(state_cache)


def get_latest_price():
    """获取最新价格（优先5m周期，其次任意周期）"""
    for tf in ['5m', '15m', '30m', '1h', '4h']:
        if tf in state_cache and state_cache[tf]:
            return state_cache[tf]['price']
    return None


# ============== 启动后台监控 ==============
_monitor_task = None


def start_monitor_in_background():
    """在后台线程中启动监控循环"""
    global _monitor_task
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def run():
        loop.run_until_complete(run_monitor())

    import threading
    t = threading.Thread(target=run, daemon=True)
    t.start()
    _monitor_task = t
    return t
