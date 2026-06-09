#!/usr/bin/env python3
"""ETH EMA 预警系统 - Web 管理界面"""
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response
import monitor as mon
import time
import os

app = Flask(__name__)
app.secret_key = 'eth-ema-alert-key'

# 启动监控线程
mon.start_monitor_in_background()
# 等一下让数据先获取
time.sleep(3)


@app.after_request
def add_no_cache_headers(response):
    """给所有响应添加禁止缓存的头"""
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0, post-check=0, pre-check=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/')
def dashboard():
    """仪表盘"""
    cfg = mon.load_config()
    states = mon.get_all_states()
    enabled_tfs = cfg['ema_alert'].get('enabled_timeframes', ['30m', '1h'])
    latest_price = mon.get_latest_price()

    tf_states = {}
    for tf in mon.TIMEFRAMES:
        if tf in states and states[tf]:
            s = states[tf]
            tf_states[tf] = {
                'label': s['label'], 'price': round(s['price'], 2),
                'ema_short': round(s['ema_short'], 2),
                'ema_long': round(s['ema_long'], 2),
                'position_text': s['position_text'],
                'arrangement_text': s['arrangement_text'],
                'signal_text': s['signal_text'],
                'signal': s['signal'], 'in_zone': s['in_zone'],
                'update_time': s.get('update_time', '--:--:--'),
            }
        else:
            tf_states[tf] = {
                'label': mon.TF_LABELS.get(tf, tf),
                'price': 0, 'ema_short': 0, 'ema_long': 0,
                'position_text': '加载中...',
                'arrangement_text': '--',
                'signal_text': '加载中...',
                'signal': 'loading', 'in_zone': False,
                'update_time': '--:--:--',
            }

    default_tf = enabled_tfs[0] if enabled_tfs else '30m'
    history = mon.load_history()
    recent_alerts = history[-5:]
    recent_alerts.reverse()

    resp = make_response(render_template(
        'dashboard.html', latest_price=latest_price, tf_states=tf_states,
        timeframes=mon.TIMEFRAMES, tf_labels=mon.TF_LABELS,
        enabled_tfs=enabled_tfs, default_tf=default_tf,
        ema_short=cfg['ema_alert']['ema_short'],
        ema_long=cfg['ema_alert']['ema_long'],
        recent_alerts=recent_alerts))
    return resp


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    """设置页 - 只保留核心配置：勾选预警周期 + EMA参数 + 邮箱"""
    cfg = mon.load_config()

    if request.method == 'POST':
        # 周期勾选（复选框返回 list，没勾选返回空 - 允许全不选，表示所有周期都不发邮件）
        enabled = request.form.getlist('enabled_timeframes')
        enabled = [x for x in enabled if x in mon.TIMEFRAMES]

        # EMA参数
        try:
            ema_short = int(request.form.get('ema_short', 180))
            ema_long = int(request.form.get('ema_long', 250))
            cooldown = int(request.form.get('cooldown_seconds', 600))
        except ValueError:
            ema_short, ema_long, cooldown = 180, 250, 600

        if ema_short >= ema_long:
            ema_short, ema_long = min(ema_short, ema_long), max(ema_short, ema_long)

        # 邮箱
        cfg['ema_alert']['enabled_timeframes'] = enabled
        cfg['ema_alert']['ema_short'] = ema_short
        cfg['ema_alert']['ema_long'] = ema_long
        cfg['alert']['cooldown_seconds'] = cooldown

        email_cfg = cfg['email']
        email_cfg['smtp_server'] = request.form.get('smtp_server', email_cfg.get('smtp_server', ''))
        try:
            email_cfg['smtp_port'] = int(request.form.get('smtp_port', 465))
        except ValueError:
            pass
        email_cfg['from_email'] = request.form.get('from_email', email_cfg.get('from_email', ''))
        email_cfg['password'] = request.form.get('password', email_cfg.get('password', ''))
        email_cfg['username'] = request.form.get('username', email_cfg.get('from_email', ''))
        email_cfg['to_email'] = request.form.get('to_email', email_cfg.get('to_email', ''))
        email_cfg['use_ssl'] = True

        mon.save_config(cfg)
        flash('✅ 设置已保存', 'success')
        return redirect(url_for('settings'))

    # GET请求
    enabled_tfs = cfg['ema_alert'].get('enabled_timeframes', ['30m', '1h'])
    return render_template('settings.html',
                           cfg=cfg,
                           timeframes=mon.TIMEFRAMES,
                           tf_labels=mon.TF_LABELS,
                           enabled_tfs=enabled_tfs,
                           active_page='settings')


@app.route('/history')
def history_page():
    """预警历史"""
    history = mon.load_history()
    history.reverse()  # 最新在前
    return render_template('history.html',
                           alerts=history[:30],
                           tf_labels=mon.TF_LABELS,
                           active_page='history')


@app.route('/api/state')
def api_state():
    """供前端刷新数据用"""
    cfg = mon.load_config()
    states = mon.get_all_states()
    enabled_tfs = cfg['ema_alert'].get('enabled_timeframes', [])
    
    result = {}
    for tf in mon.TIMEFRAMES:
        if tf in states and states[tf]:
            s = states[tf]
            result[tf] = {
                'price': round(s['price'], 2),
                'ema_short': round(s['ema_short'], 2),
                'ema_long': round(s['ema_long'], 2),
                'signal_text': s['signal_text'],
                'position_text': s['position_text'],
                'arrangement_text': s['arrangement_text'],
                'in_zone': s['in_zone'],
                'update_time': s.get('update_time', ''),
                'enabled': tf in enabled_tfs,
            }
    return jsonify({'price': mon.get_latest_price(), 'states': result})


@app.route('/settings/test-email', methods=['POST'])
def test_email():
    """发送测试邮件"""
    cfg = mon.load_config()
    subject = '✅ ETH EMA 预警系统测试邮件'
    body = '<html><body style="font-family:sans-serif;padding:20px;"><h2>✅ 邮件配置正常</h2><p>收到这封邮件说明你的邮箱配置是对的。</p></body></html>'
    if mon.send_email(subject, body, cfg):
        flash('✅ 测试邮件已发送，请检查邮箱', 'success')
    else:
        flash('❌ 邮件发送失败，请检查邮箱配置', 'error')
    return redirect(url_for('settings'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 ETH EMA 预警系统 Web 管理界面启动 (端口: {port})")
    print(f"   访问: http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
