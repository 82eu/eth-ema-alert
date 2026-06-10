#!/usr/bin/env python3
"""ETH EMA 预警系统 - Web 界面"""
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash
import monitor as mon
import time, os, json

app = Flask(__name__)
app.secret_key = 'eth-ema-alert-key'

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
        if s and isinstance(s, dict) and s.get('price'):
            tf_states[tf] = {
                'ema_short': float(s.get('ema_short', 0)),
                'ema_long': float(s.get('ema_long', 0)),
                'arrangement_text': s.get('arrangement_text', ''),
                'in_zone': bool(s.get('in_zone', False)),
                'update_time': s.get('update_time', ''),
                'data_source': s.get('data_source', ''),
                'enabled': tf in enabled_tfs,
                'price': float(s.get('price', 0)),
            }
        else:
            tf_states[tf] = {
                'ema_short': 0, 'ema_long': 0,
                'arrangement_text': '', 'in_zone': False,
                'update_time': '', 'data_source': '', 'enabled': tf in enabled_tfs,
                'price': 0,
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
        return jsonify({
            'price': mon.get_latest_price(),
            'states': result,
            'last_update': mon.get_last_update_time(),
            'connection': mon.get_connection_status(),
            'source_health': mon.get_source_health(),
            'price_ranges': price_ranges,
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


@app.route('/test_alert_email', methods=['POST'])
def test_alert_email():
    """仪表盘一键发送模拟预警邮件（返回JSON，显示真实错误）"""
    try:
        cfg = mon.load_config()
        email_cfg = cfg.get('email', {})
        if not email_cfg.get('smtp_server') or not email_cfg.get('to_email') or not email_cfg.get('from_email') or not email_cfg.get('password'):
            return jsonify({'success': False, 'error': '邮箱配置不完整，请在 Render 的 Environment 中设置 ALERT_FROM_EMAIL/ALERT_TO_EMAIL/ALERT_EMAIL_PASSWORD/ALERT_SMTP_SERVER'})

        import smtplib, ssl
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        # 取当前最新数据做模拟邮件内容
        try:
            state = mon.analyze('15m', cfg['ema_alert']['ema_short'], cfg['ema_alert']['ema_long'])
            price = state.get('price', 3500)
            ema_s = state.get('ema_short', 3500)
            ema_l = state.get('ema_long', 3500)
            signal = state.get('signal_text', '测试信号')
        except Exception:
            price, ema_s, ema_l, signal = 3500, 3520, 3480, '测试预警'

        subject = '[ETH EMA 预警] 15m 周期 · %s · $%.2f' % (signal, float(price))
        html_body = """
        <html><body style="font-family: sans-serif; max-width: 500px; margin: 0 auto; padding: 20px;">
            <h2 style="color:#f97316;">%s</h2>
            <div style="background:#f8fafc; padding: 15px; border-radius: 8px; margin-top: 10px;">
                <div style="font-size: 28px; font-weight: bold; text-align: center;">$%.2f</div>
                <div style="margin-top: 15px; font-size: 16px;">
                    <div>信号类型：<b>%s</b></div>
                    <div>EMA%d：<b>$%.2f</b></div>
                    <div>EMA%d：<b>$%.2f</b></div>
                </div>
            </div>
            <div style="margin-top: 15px; color: #64748b; font-size: 12px; text-align: center;">
                触发时间：%s · 周期：15m · 测试邮件
            </div>
        </body></html>
        """ % (subject, float(price), signal,
               cfg['ema_alert']['ema_short'], float(ema_s),
               cfg['ema_alert']['ema_long'], float(ema_l),
               time.strftime('%Y-%m-%d %H:%M:%S'))

        msg = MIMEMultipart("alternative")
        msg['Subject'] = subject
        msg['From'] = email_cfg.get('from_email', '')
        msg['To'] = email_cfg.get('to_email', '')
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        smtp_server = email_cfg.get('smtp_server', 'smtp.gmail.com')
        smtp_port = int(email_cfg.get('smtp_port', 465))
        from_addr = email_cfg.get('from_email', '')
        to_addr = email_cfg.get('to_email', '')
        username = email_cfg.get('username', from_addr)
        password = email_cfg.get('password', '')

        mon.logger.info('尝试发送邮件: server=%s port=%d from=%s to=%s' % (smtp_server, smtp_port, from_addr, to_addr))

        context = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context, timeout=20) as server:
                server.login(username, password)
                server.sendmail(from_addr, [to_addr], msg.as_string())
        except Exception as e:
            # 如果 SSL 465 失败，尝试 STARTTLS 587
            mon.logger.warning('SSL 465失败，尝试STARTTLS 587: %s' % e)
            with smtplib.SMTP(smtp_server, 587, timeout=20) as server:
                server.starttls(context=context)
                server.login(username, password)
                server.sendmail(from_addr, [to_addr], msg.as_string())

        mon.logger.info('✅ 测试预警邮件已发送: %s' % subject)
        return jsonify({'success': True, 'message': '✅ 邮件发送成功！请检查手机QQ邮箱（约1-5分钟收到），MacroDroid 会检测到并触发闹钟。'})
    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        mon.logger.error('测试预警邮件发送失败: %s\n%s' % (e, err_detail))
        return jsonify({'success': False, 'error': str(e), 'detail': err_detail.splitlines()[-1] if err_detail else str(e)})


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
