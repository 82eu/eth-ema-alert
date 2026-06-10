#!/usr/bin/env python3
"""ETH EMA 预警系统 - Web 界面（稳定版）"""
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash
import monitor as mon
import time, os, json

app = Flask(__name__)
app.secret_key = 'eth-ema-alert-key'

# ====== 全局错误处理器 ======
# 任何未捕获的异常都返回 JSON（绝对不返回空白的 500 页）
@app.errorhandler(Exception)
def _global_error_handler(e):
    import traceback
    tb = traceback.format_exc()
    mon.logger.error('未捕获异常: %s\n%s' % (str(e), tb))
    path = request.path if hasattr(request, 'path') else ''
    if '/api/' in path or 'test_alert_email' in path:
        return jsonify({'success': False, 'error': str(e), 'detail': tb.splitlines()[-1] if tb else str(e)}), 500
    return '服务错误: ' + str(e), 500

mon.start_monitor_in_background()
time.sleep(3)


@app.after_request
def add_no_cache_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def _get_states_dict():
    """组装 dashboard 所需的 states 数据"""
    cfg = mon.load_config()
    enabled_tfs = set(cfg.get('ema_alert', {}).get('enabled_timeframes', []))
    raw_states = mon.get_all_states() or {}
    tf_states = {}
    for tf in mon.TIMEFRAMES:
        s = raw_states.get(tf)
        if s and isinstance(s, dict) and s.get('price') and float(s.get('price', 0)) > 0:
            tf_states[tf] = {
                'ema_short': float(s.get('ema_short', 0)),
                'ema_long': float(s.get('ema_long', 0)),
                'arrangement_text': s.get('arrangement_text', ''),
                'in_zone': bool(s.get('in_zone', False)),
                'update_time': s.get('update_time', ''),
                'data_source': s.get('data_source', ''),
                'enabled': tf in enabled_tfs,
                'price': float(s.get('price', 0)),
                'stale': bool(s.get('_stale', False)),
            }
        else:
            tf_states[tf] = {
                'ema_short': 0, 'ema_long': 0,
                'arrangement_text': '加载中', 'in_zone': False,
                'update_time': '等待数据...', 'data_source': 'loading',
                'enabled': tf in enabled_tfs, 'price': 0, 'stale': True,
            }
    return tf_states, enabled_tfs


@app.route('/')
def dashboard():
    """仪表盘"""
    try:
        cfg = mon.load_config()
        ema_short_cfg = cfg['ema_alert'].get('ema_short', 180)
        ema_long_cfg = cfg['ema_alert'].get('ema_long', 250)
        tf_states, enabled_tfs = _get_states_dict()
        recent_alerts = mon.get_recent_alerts(limit=8)
        price_ranges = cfg.get('price_ranges', []) or []

        return render_template('dashboard.html',
            latest_price=mon.get_latest_price(),
            ema_short=ema_short_cfg,
            ema_long=ema_long_cfg,
            timeframes=mon.TIMEFRAMES,
            tf_labels=mon.TF_LABELS,
            default_tf='15m',
            enabled_tfs=enabled_tfs,
            tf_states=tf_states,
            recent_alerts=recent_alerts,
            connection=mon.get_connection_status(),
            source_health=mon.get_source_health(),
            price_ranges_json=json.dumps(price_ranges, ensure_ascii=False),
        )
    except Exception as e:
        import traceback
        mon.logger.error("dashboard 渲染失败: %s\n%s", e, traceback.format_exc())
        return "页面加载错误: " + str(e), 500


@app.route('/api/state')
def api_state():
    """返回当前状态（JSON）"""
    try:
        cfg = mon.load_config()
        tf_states, enabled_tfs = _get_states_dict()
        result = {}
        for tf in mon.TIMEFRAMES:
            s = tf_states[tf]
            result[tf] = {
                'price': s['price'],
                'ema_short': s['ema_short'],
                'ema_long': s['ema_long'],
                'arrangement_text': s['arrangement_text'],
                'in_zone': s['in_zone'],
                'update_time': s['update_time'],
                'enabled': s['enabled'],
                'data_source': s['data_source'],
            }
        price_ranges = cfg.get('price_ranges', []) or []
        # 环境变量诊断（不打印密码，只打印是否存在）
        email_cfg = cfg.get('email', {})
        feishu_cfg = cfg.get('feishu', {})
        env_diag = {
            'ALERT_FROM_EMAIL_set': bool(os.environ.get('ALERT_FROM_EMAIL')),
            'ALERT_TO_EMAIL_set': bool(os.environ.get('ALERT_TO_EMAIL')),
            'ALERT_EMAIL_PASSWORD_set': bool(os.environ.get('ALERT_EMAIL_PASSWORD')),
            'ALERT_SMTP_SERVER_set': bool(os.environ.get('ALERT_SMTP_SERVER')),
            'ALERT_SMTP_PORT_set': bool(os.environ.get('ALERT_SMTP_PORT')),
            'FEISHU_WEBHOOK_set': bool(os.environ.get('FEISHU_WEBHOOK')),
            'cfg_feishu_webhook_set': bool(feishu_cfg.get('webhook')),
            'cfg_smtp_server': email_cfg.get('smtp_server', ''),
            'cfg_from_email_masked': email_cfg.get('from_email', '')[:10] + '...' if email_cfg.get('from_email') else '(empty)',
            'cfg_to_email_masked': email_cfg.get('to_email', '')[:10] + '...' if email_cfg.get('to_email') else '(empty)',
            'cfg_password_len': len(email_cfg.get('password', '')),
        }
        return jsonify({
            'price': mon.get_latest_price(),
            'states': result,
            'last_update': mon.get_last_update_time(),
            'connection': mon.get_connection_status(),
            'source_health': mon.get_source_health(),
            'price_ranges': price_ranges,
            'env_diag': env_diag,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/price_ranges', methods=['POST'])
def api_price_ranges():
    """价格区间管理"""
    try:
        data = request.get_json(force=True) or {}
        action = data.get('action', '')
        cfg = mon.load_config()
        price_ranges = cfg.get('price_ranges', []) or []

        if action == 'add':
            r = data.get('range', {})
            name = str(r.get('name', '')).strip()
            low = float(r.get('low', 0))
            high = float(r.get('high', 0))
            if not name or low <= 0 or high <= low:
                return jsonify({'success': False, 'error': '参数无效'}), 400
            price_ranges.append({'name': name, 'low': low, 'high': high, 'enabled': bool(r.get('enabled', True))})
            cfg['price_ranges'] = price_ranges
            mon.save_config(cfg)
            return jsonify({'success': True, 'price_ranges': price_ranges})

        elif action == 'toggle':
            idx = int(data.get('index', -1))
            if 0 <= idx < len(price_ranges):
                price_ranges[idx]['enabled'] = not price_ranges[idx].get('enabled', False)
                cfg['price_ranges'] = price_ranges
                mon.save_config(cfg)
                return jsonify({'success': True, 'price_ranges': price_ranges})
            return jsonify({'success': False, 'error': '索引无效'}), 404

        elif action == 'remove':
            idx = int(data.get('index', -1))
            if 0 <= idx < len(price_ranges):
                del price_ranges[idx]
                cfg['price_ranges'] = price_ranges
                mon.save_config(cfg)
                return jsonify({'success': True, 'price_ranges': price_ranges})
            return jsonify({'success': False, 'error': '索引无效'}), 404

        return jsonify({'success': False, 'error': '未知操作'}), 400
    except Exception as e:
        mon.logger.error("价格区间 API 错误: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/alerts/<path:ts>', methods=['DELETE'])
def api_delete_alert(ts):
    """删除指定预警"""
    try:
        ok = mon.delete_alert_by_timestamp(float(ts))
        return jsonify({'success': ok})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/alerts/clear', methods=['POST'])
def api_clear_alerts():
    """清空所有预警"""
    try:
        ok = mon.clear_all_alerts()
        return jsonify({'success': ok})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    """设置页面"""
    try:
        cfg = mon.load_config()
        if request.method == 'POST':
            if 'ema_short' in request.form:
                try:
                    cfg['ema_alert']['ema_short'] = int(request.form['ema_short'])
                    cfg['ema_alert']['ema_long'] = int(request.form['ema_long'])
                    cfg['alert']['cooldown_seconds'] = int(request.form.get('cooldown_seconds', cfg['alert'].get('cooldown_seconds', 600)))
                except (ValueError, KeyError):
                    pass
            enabled = request.form.getlist('enabled_timeframes')
            if enabled:
                cfg['ema_alert']['enabled_timeframes'] = list(set(enabled))
            if 'smtp_server' in request.form and request.form['smtp_server']:
                cfg['email']['smtp_server'] = request.form['smtp_server']
                try:
                    cfg['email']['smtp_port'] = int(request.form.get('smtp_port', 465))
                except ValueError:
                    pass
                cfg['email']['from_email'] = request.form.get('from_email', '')
                cfg['email']['password'] = request.form.get('password', '')
                cfg['email']['to_email'] = request.form.get('to_email', '')
                cfg['email']['use_ssl'] = True
            mon.save_config(cfg)
            flash('✅ 配置已保存', 'success')
            return redirect(url_for('settings'))

        if 'email' not in cfg:
            cfg['email'] = {'smtp_server': '', 'smtp_port': 465, 'from_email': '', 'password': '', 'to_email': ''}
        if 'alert' not in cfg:
            cfg['alert'] = {'cooldown_seconds': 600}

        return render_template('settings.html',
            cfg=cfg,
            timeframes=mon.TIMEFRAMES,
            tf_labels=mon.TF_LABELS,
            enabled_tfs=set(cfg['ema_alert'].get('enabled_timeframes', [])))
    except Exception as e:
        import traceback
        mon.logger.error("设置错误: %s\n%s", e, traceback.format_exc())
        return "错误: " + str(e), 500


@app.route('/test_email', methods=['POST'])
def test_email():
    """发送测试邮件"""
    try:
        cfg = mon.load_config()
        email_cfg = cfg.get('email', {})
        if not email_cfg.get('smtp_server') or not email_cfg.get('to_email'):
            flash('❌ 请先填写完整的邮箱配置', 'error')
            return redirect(url_for('settings'))

        import smtplib, ssl
        from email.mime.text import MIMEText
        msg = MIMEText('ETH EMA 预警系统测试邮件。\n\n系统正常运行中...\n' + time.strftime('%Y-%m-%d %H:%M:%S'), 'plain', 'utf-8')
        msg['Subject'] = '[ETH EMA 预警] 测试邮件'
        msg['From'] = email_cfg.get('from_email', '')
        msg['To'] = email_cfg.get('to_email', '')
        port = int(email_cfg.get('smtp_port', 465))
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(email_cfg.get('smtp_server'), port, context=context, timeout=30) as server:
                server.login(email_cfg.get('username', email_cfg.get('from_email', '')), email_cfg.get('password', ''))
                server.sendmail(msg['From'], [msg['To']], msg.as_string())
        else:
            with smtplib.SMTP(email_cfg.get('smtp_server'), port, timeout=30) as server:
                server.starttls(context=context)
                server.login(email_cfg.get('username', email_cfg.get('from_email', '')), email_cfg.get('password', ''))
                server.sendmail(msg['From'], [msg['To']], msg.as_string())
        flash('✅ 测试邮件已发送，请检查收件箱（可能在垃圾箱）', 'success')
    except Exception as e:
        import traceback
        mon.logger.error("测试邮件失败: %s\n%s", e, traceback.format_exc())
        flash('❌ 发送失败: ' + str(e), 'error')
    return redirect(url_for('settings'))


@app.route('/test_alert_email', methods=['GET', 'POST'])
def test_alert_email():
    """一键发送模拟预警（优先飞书，失败再邮件）"""
    cfg = mon.load_config()

    # 获取当前价格
    try:
        raw_states = mon.get_all_states() or {}
        s = raw_states.get('15m') or raw_states.get('5m') or {}
        price = float(s.get('price', 0)) if isinstance(s, dict) and s.get('price') else 0
        if price <= 0: price = float(mon.get_latest_price() or 0)
    except Exception:
        price = 0
    price_text = ('$%.2f' % price) if price > 0 else '系统运行中'

    subject = '[ETH EMA 预警] 测试消息 · %s' % price_text

    # 1. 先尝试飞书
    feishu_url = os.environ.get('FEISHU_WEBHOOK', '').strip()
    if not feishu_url:
        feishu_cfg = cfg.get('feishu', {}) if isinstance(cfg.get('feishu'), dict) else {}
        feishu_url = feishu_cfg.get('webhook', '').strip()

    body_text_plain = f"ETH EMA 预警系统测试\n\n当前价格: {price_text}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n如果你收到这条消息，说明推送配置正常！"
    body_html_plain = f"<html><body><h2>ETH EMA 预警系统测试</h2><p>当前价格: <b>{price_text}</b></p><p>时间: {time.strftime('%Y-%m-%d %H:%M:%S')}</p><p>如果你收到这封邮件，说明邮件配置正常！</p></body></html>"

    success = False
    msgs = []
    errs = []

    # 尝试飞书
    if feishu_url:
        try:
            import requests as req_mod
            payload = {'msg_type': 'text', 'content': {'text': f"{subject}\n\n{body_text_plain}"}}
            r = req_mod.post(feishu_url, json=payload, timeout=15)
            if r.status_code == 200:
                j = r.json() if r.content else {}
                # 飞书返回 code=0 成功
                if j.get('code') == 0 or j.get('StatusCode') == 0 or (j.get('code') is None and j.get('StatusCode') is None):
                    success = True
                    msgs.append('✅ 飞书推送成功！请检查飞书群消息。')
                else:
                    errs.append('飞书返回非成功: %s' % str(j)[:150])
            else:
                errs.append('飞书 HTTP %s: %s' % (r.status_code, r.text[:150]))
        except Exception as e:
            errs.append('飞书请求异常: %s' % str(e)[:150])
    else:
        errs.append('未配置 FEISHU_WEBHOOK')

    # 2. 如果飞书失败或没配置，试邮件
    if not success:
        try:
            email_cfg = cfg.get('email', {})
            has_email = email_cfg.get('smtp_server') and email_cfg.get('from_email') and email_cfg.get('password') and email_cfg.get('to_email')
            if has_email:
                if mon.send_email(subject, body_html_plain, cfg):
                    success = True
                    msgs.append('✅ 邮件发送成功！（飞书不可用，已自动降级到邮件）')
                else:
                    errs.append('邮件发送失败（SMTP 可能无法连接）')
            else:
                errs.append('邮件配置不完整')
        except Exception as e:
            errs.append('邮件异常: %s' % str(e)[:100])

    if success:
        return jsonify({'success': True, 'message': ' | '.join(msgs)})
    return jsonify({'success': False, 'error': ' | '.join(errs), 'detail': '建议：1) 在 Render Environment 设置 FEISHU_WEBHOOK；2) 飞书机器人需加入群；3) 如设置了关键词验证，消息里需包含该关键词。'})


@app.route('/history')
def history_page():
    """历史预警记录"""
    try:
        alerts = mon.get_recent_alerts(limit=50)
        return render_template('history.html', alerts=alerts)
    except Exception as e:
        import traceback
        mon.logger.error("历史页面错误: %s\n%s", e, traceback.format_exc())
        return "错误: " + str(e), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("ETH EMA 预警系统启动 (端口: %d)" % port)
    app.run(host='0.0.0.0', port=port, debug=False)
