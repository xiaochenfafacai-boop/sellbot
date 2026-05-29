import logging
import sqlite3
import json
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import re
import io
import csv
import threading
import random
from flask import Flask, request, jsonify
import os

# 配置日志
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==========================================
# 👑 商业授权全局配置 (你的绝对控制信息)
# ==========================================
SYSTEM_ADMIN_ID = 8782394486        # 你的个人ID
USDT_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw" # 你的最新收款地址

TOKEN = "8915202728:AAGR4QS4_iYbwqR6nIE68N_pykVeoTBVBUA" # 💡 卖给新客户时在这里换上新Bot的Token
WEB_URL = "https://mybot-6ghty.onrender.com"
PORT = int(os.environ.get('PORT', 8080))

TIMEZONES = {
    'china': 'Asia/Shanghai',
    'myanmar': 'Asia/Yangon',
    'thailand': 'Asia/Bangkok',
}

flask_app = Flask(__name__)

# ==========================================
# 💾 数据库函数 (合并原版账单库与授权库)
# ==========================================

def get_current_time(timezone_str):
    try:
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    except:
        tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    # 原版群设置表
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY,
                  operators TEXT DEFAULT '[]',
                  exchange_rate REAL DEFAULT 7.2,
                  fee_rate REAL DEFAULT 0,
                  is_active INTEGER DEFAULT 0,
                  language TEXT DEFAULT 'chinese',
                  timezone TEXT DEFAULT 'Asia/Shanghai',
                  show_usdt INTEGER DEFAULT 1)''')
    # 原版记账流水表
    c.execute('''CREATE TABLE IF NOT EXISTS bills
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id INTEGER,
                  user_id INTEGER,
                  username TEXT,
                  remark TEXT,
                  amount REAL,
                  usdt_amount REAL,
                  exchange_rate REAL,
                  bill_type TEXT,
                  timestamp TEXT,
                  date_str TEXT,
                  is_settled INTEGER DEFAULT 0)''')
    
    # 🌟 新增：商业授权专属表 (用来存机器人的老板是谁、何时到期)
    c.execute('''CREATE TABLE IF NOT EXISTS bot_licenses
                 (bot_token TEXT PRIMARY KEY,
                  owner_id INTEGER DEFAULT NULL,       
                  owner_username TEXT DEFAULT NULL,   
                  expire_time TEXT DEFAULT '2026-06-15', 
                  last_remind_date TEXT DEFAULT NULL)''')
    conn.commit()
    conn.close()

# ---- 商业授权独立底层方法 ----
def get_bot_license(token):
    conn = sqlite3.connect('bot_data.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM bot_licenses WHERE bot_token = ?", (token,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    # 首次开机无数据则自动初始化
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("INSERT INTO bot_licenses (bot_token) VALUES (?)", (token,))
    conn.commit()
    conn.close()
    return {"bot_token": token, "owner_id": None, "owner_username": None, "expire_time": "2026-06-15", "last_remind_date": None}

def bind_bot_owner(token, uid, username):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("UPDATE bot_licenses SET owner_id = ?, owner_username = ? WHERE bot_token = ?", (uid, username, token))
    conn.commit()
    conn.close()

def update_last_remind_date(token, date_str):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("UPDATE bot_licenses SET last_remind_date = ? WHERE bot_token = ?", (date_str, token))
    conn.commit()
    conn.close()

# ---- 原版记账流水核心方法 ----
def get_setting(group_id, key):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    cols = ['group_id', 'operators', 'exchange_rate', 'fee_rate', 'is_active', 'language', 'timezone', 'show_usdt']
    return dict(zip(cols, row)).get(key)

def update_setting(group_id, key, value):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
    if c.fetchone():
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
    else:
        c.execute("INSERT INTO settings (group_id, operators, exchange_rate, fee_rate, is_active, language, timezone, show_usdt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (group_id, '[]', 7.2, 0, 0, 'chinese', 'Asia/Shanghai', 1))
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
    conn.commit()
    conn.close()

def is_master(user_id):
    return user_id == MASTER_USER_ID or user_id == SYSTEM_ADMIN_ID

def is_operator(group_id, user_id):
    ops = json.loads(get_setting(group_id, 'operators') or '[]')
    return user_id in ops or user_id == SYSTEM_ADMIN_ID

def can_use(group_id, user_id):
    return is_master(user_id) or is_operator(group_id, user_id)

def add_bill(group_id, user_id, username, remark, amount, bill_type, exchange_rate=None):
    if exchange_rate is None:
        exchange_rate = get_setting(group_id, 'exchange_rate') or 7.2
    if bill_type == 'income':
        usdt_amount = amount / exchange_rate
    else:
        usdt_amount = amount
    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, full_time = get_current_time(tz_str)
    date_str = now.strftime("%Y-%m-%d")
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute('''INSERT INTO bills 
                 (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, timestamp, date_str, is_settled)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''',
              (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, full_time, date_str))
    conn.commit()
    conn.close()
    return usdt_amount

def get_class_bills_by_date(group_id, target_date):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT remark, username, amount, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' ORDER BY id DESC", (group_id, target_date))
    income = c.fetchall()
    c.execute("SELECT remark, username, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense' ORDER BY id DESC", (group_id, target_date))
    expense = c.fetchall()
    c.execute("SELECT SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income'", (group_id, target_date))
    total_income = c.fetchone()
    c.execute("SELECT SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense'", (group_id, target_date))
    total_expense = c.fetchone()
    conn.close()
    return income, expense, total_income, total_expense

def settle_today_bills(group_id, target_date):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("UPDATE bills SET is_settled = 1 WHERE group_id = ? AND date_str = ?", (group_id, target_date))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated

def delete_today_bills(group_id):
    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (group_id, today_date))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def delete_last_bill(group_id):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT id FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 1", (group_id,))
    last = c.fetchone()
    if last:
        c.execute("DELETE FROM bills WHERE id = ?", (last[0],))
        deleted = 1
    else:
        deleted = 0
    conn.commit()
    conn.close()
    return deleted

def delete_all_bills(group_id):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ?", (group_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def delete_user_bills(group_id, name):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ? AND (LOWER(username) = ? OR LOWER(remark) = ?)", (group_id, name.lower(), name.lower()))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

# ==========================================
# ⌨️ UI 专属授权控制面板键盘 (8个按钮布局)
# ==========================================
def get_expired_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🎁 试用", callback_data="btn_trial"),
            InlineKeyboardButton("🚀 开始", callback_data="btn_start")
        ],
        [
            InlineKeyboardButton("📅 到期时间", callback_data="btn_expire_time"),
            InlineKeyboardButton("📖 详细说明书", callback_data="btn_docs")
        ],
        [
            InlineKeyboardButton("🔄 自动续费", callback_data="btn_renew"),
            InlineKeyboardButton("👑 设置权限人", callback_data="btn_set_admin")
        ],
        [
            InlineKeyboardButton("👤 设置操作人", callback_data="btn_set_operator"),
            InlineKeyboardButton("⚙️ 开局/关闭计算功能", callback_data="btn_toggle_calc")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ==========================================
# Web 页面接口 (保持原样不动)
# ==========================================
@flask_app.route('/')
def index():
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>课时历史账单系统</title><style>*{margin:0;padding:0;box-sizing:border-box;}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;background:#f0f2f5;padding:20px;}.container{max-width:1400px;margin:0 auto;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);overflow:hidden;}.header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:24px 30px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:15px;}.header-text{flex:1;}.header h1{font-size:28px;margin-bottom:8px;}.date-picker-box{background:rgba(255,255,255,0.2);padding:10px 15px;border-radius:8px;color:white;}.date-picker-box label{font-size:14px;margin-right:8px;font-weight:bold;}.date-picker-box input{border:none;padding:6px 10px;border-radius:4px;font-size:14px;outline:none;}.content{padding:24px 30px;}.section{margin-bottom:32px;}.section-title{font-size:18px;font-weight:600;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #667eea;}table{width:100%;border-collapse:collapse;font-size:14px;}th,td{padding:12px 10px;text-align:left;border-bottom:1px solid #eef2f6;}th{background:#f8f9fc;font-weight:600;}.stats-box{background:linear-gradient(135deg,#f8f9fc 0%,#f0f2f5 100%);border-radius:12px;padding:24px;margin-top:20px;}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;}.stat-card{background:white;padding:16px;border-radius:12px;text-align:center;}.stat-label{font-size:12px;color:#888;margin-bottom:8px;}.stat-value{font-size:24px;font-weight:700;color:#333;}.stat-item{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eef2f6;}.stat-name{font-weight:500;color:#333;}.stat-number{color:#667eea;font-weight:600;}.loading{text-align:center;padding:50px;color:#888を確認}</style></head><body><div class="container"><div class="header"><div class="header-text"><h1>📋 实时课堂账单历史明细</h1><p id="dateInfo">默认同步实时账单</p></div><div class="date-picker-box"><label>📅 选择账单日期:</label><input type="date" id="targetDate" onchange="onDateChange()"></div></div><div class="content" id="content"><div class="loading">正在同步实时账单...</div></div></div><script>let GROUP_ID=null;let currentSelectedDate="";const today=new Date();const yyyy=today.getFullYear();let mm=today.getMonth()+1;let dd=today.getDate();if(mm<10)mm='0'+mm;if(dd<10)dd='0'+dd;currentSelectedDate=`${yyyy}-${mm}-${dd}`;document.getElementById('targetDate').value=currentSelectedDate;function getGroupID(){const urlParams=new URLSearchParams(window.location.search);GROUP_ID=urlParams.get('group_id');if(!GROUP_ID){document.getElementById('content').innerHTML='<div class="loading">❌ 请通过机器人的 "查看完整账单" 按钮访问</div>';return false;}return true;}function onDateChange(){currentSelectedDate=document.getElementById('targetDate').value;loadData();}async function loadData(){if(!GROUP_ID)return;try{const response=await fetch(`/api/bill?group_id=${GROUP_ID}&date=${currentSelectedDate}`);const data=await response.json();if(data.error||(!data.income_bills.length&&!data.expense_bills.length)){document.getElementById('content').innerHTML=`<div class="loading">📅 ${currentSelectedDate} 暂无账单数据记录</div>`;return;}let suffix=data.show_usdt?' USDT':'';let html='';if(data.income_bills&&data.income_bills.length>0){html+=`<div class="section"><div class="section-title">📥 入款记录 (${data.income_bills.length} 笔)</div><table><thead><tr><th>备注</th><th>时间</th><th>金额(元)</th><th>汇率</th><th>等值数量</th><th>操作人</th></tr></thead><tbody>`;for(const bill of data.income_bills){html+=`<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td>${bill.amount}</td><td>${bill.exchange_rate}</td><td>${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`;}html+=`</tbody></table></div>`;}if(data.expense_bills&&data.expense_bills.length>0){html+=`<div class="section"><div class="section-title">📤 下发记录 (${data.expense_bills.length} 笔)</div><table><thead><tr><th>备注</th><th>时间</th><th>下发数量</th><th>操作人</th></tr></thead><tbody>`;for(const bill of data.expense_bills){html+=`<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td>${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`;}html+=`</tbody></table></div>`;}if(data.remark_stats&&data.remark_stats.length>0){html+=`<div class="section"><div class="section-title">📊 备注分类统计</div>`;for(const stat of data.remark_stats){html+=`<div class="stat-item"><span class="stat-name">📝 ${stat.remark}</span><span class="stat-number">${stat.count}笔 | ${stat.amount}元 | ${stat.usdt}${suffix}</span></div>`;}html+=`</div>`;}html+=`<div class="stats-box"><div class="stats-grid"><div class="stat-card"><div class="stat-label">💰 费率</div><div class="stat-value">${data.fee_rate}%</div></div><div class="stat-card"><div class="stat-label">💱 汇率</div><div class="stat-value">${data.exchange_rate}</div></div><div class="stat-card"><div class="stat-label">📥 总入款(元)</div><div class="stat-value">${data.total_rmb}</div></div><div class="stat-card"><div class="stat-label">💵 总入款数量</div><div class="stat-value">${data.total_usdt}${suffix}</div></div><div class="stat-card"><div class="stat-label">📤 已下发</div><div class="stat-value">${data.expense_usdt}${suffix}</div></div><div class="stat-card"><div class="stat-label">📊 未下发</div><div class="stat-value">${data.remaining_usdt}${suffix}</div></div></div></div>`;document.getElementById('content').innerHTML=html;}catch(err){document.getElementById('content').innerHTML='<div class="loading">❌ 数据解析错误或网络异常，请重新从群内打开链接</div>';}}if(getGroupID()){loadData();setInterval(()=>{const t=new Date();let m=t.getMonth()+1;let d=t.getDate();if(m<10)m='0'+m;if(d<10)d='0'+d;if(currentSelectedDate===`${t.getFullYear()}-${m}-${d}`){loadData();}},4000);}</script></body></html>'''

@flask_app.route('/api/bill')
def api_bill():
    try:
        group_id = request.args.get('group_id', type=int, default=0)
        tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_str = now.strftime("%Y-%m-%d")
        target_date = request.args.get('date', default=today_str)
        
        income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)
        rate = get_setting(group_id, 'exchange_rate') or 7.2
        fee_rate = get_setting(group_id, 'fee_rate') or 0
        show_usdt = get_setting(group_id, 'show_usdt') or 1
        
        total_rmb = total_income[0] if (total_income and total_income[0]) else 0
        total_usdt = total_income[1] if (total_income and total_income[1]) else 0
        expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0
        
        income_bills = []
        expense_bills = []
        
        for row in income:
            remark, username, amount, usdt, ex_rate, ts = row
            time_str = ts[5:16] if (ts and len(ts) > 11) else (ts or '-')
            income_bills.append({
                'remark': remark or '-', 'username': username or '未知', 'amount': f"{amount or 0:.0f}", 
                'usdt': f"{usdt or 0:.2f}", 'exchange_rate': f"{ex_rate or rate:.2f}", 'time': time_str
            })
            
        for row in expense:
            remark, username, usdt, ex_rate, ts = row
            time_str = ts[5:16] if (ts and len(ts) > 11) else (ts or '-')
            expense_bills.append({
                'remark': remark or '-', 'username': username or '未知', 'usdt': f"{usdt or 0:.2f}", 'time': time_str
            })

        remark_stats = []
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT remark, COUNT(*), SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' GROUP BY remark ORDER BY SUM(usdt_amount) DESC", (group_id, target_date))
        for row in c.fetchall():
            remark_stats.append({
                'remark': row[0] if row[0] else '无备注', 'count': row[1] or 0, 
                'amount': f"{row[2] or 0:.0f}", 'usdt': f"{row[3] or 0:.2f}"
            })
        conn.close()
        
        return jsonify({
            'exchange_rate': f"{rate:.2f}", 'fee_rate': f"{fee_rate:.0f}", 'total_rmb': f"{total_rmb:.0f}", 
            'total_usdt': f"{total_usdt:.2f}", 'expense_usdt': f"{expense_usdt:.2f}", 
            'remaining_usdt': f"{total_usdt - expense_usdt:.2f}", 'show_usdt': int(show_usdt), 
            'income_bills': income_bills, 'expense_bills': expense_bills, 'remark_stats': remark_stats
        })
    except Exception as e:
        logging.error(f"API Error: {str(e)}")
        return jsonify({'error': True, 'msg': str(e)}), 500

# ==========================================
# 文本辅助函数 (保持原样不动)
# ==========================================
MASTER_USER_ID = 5292391547 # 兼容原逻辑中的常量定义

def get_help_text(lang):
    if lang == 'myanmar':
        return """
🤖 *စာရင်းကိုင်ဘော့ အကူအညီ* (Help)

📌 *စာရင်းသွင်းရန် ပုံစံများ：*
`+1000` - ငွေဝင် ၁၀၀၀ ကျပ်
`-1000` - ငွေဝင် -၁၀၀၀ ကျပ် (နှုတ်ရန်)
`မှတ်ချက်+2000` - မှတ်ချက်ဖြင့် ငွေသွင်းရန်
`မှတ်ချက်-2000` - မှတ်ချက်ဖြင့် ငွေနှုတ်ရန်
`ထုတ်50` - 50 USDT ထုတ်ရန် (ဒေါင်းလုပ်)
`မှတ်ချက်ထုတ်50` - မှတ်ချက်ဖြင့် 50 USDT ထုတ်ရန်
`+0` - ယနေ့စာရင်းချုပ် ကြည့်ရန်

📌 *စီမံခန့်ခွဲရေး ကွတ်ကီးများ：*
`အတန်းစက္ကူ` - စာရင်းကိုင်စနစ် ဖွင့်ခြင်း (上课)
`အတန်းဆင်း` - စာရင်းပိတ်ပြီး ရှင်းလင်းခြင်း (下课)
`ငွေလဲနှုန်း 7.2` - ငွေလဲနှုန်း သတ်မှတ်ရန်
`อော်ပရေတာခန့်ရန်` - စာရင်းကိုင်ခန့်ရန် (စာပြန်ပြီး ပို့ပါ)
`အော်ပရေတာစာရင်း` - အော်ပရေတာစာရင်း ကြည့်ရန်
`ဘာသာစကား` - ဘာသာစကားပြောင်းရန် (中文/မြန်မာ)
`အချိန်သတ်မှတ်` - အချိန်ဇုန် ပြောင်းရန်
`ယူပြရန်` - USDT ပြရန်
`ယူဝှက်ရန်` - USDT ဝှက်ရန်

📌 *ဖျက်သိမ်းခြင်း ကွတ်ကီးများ：*
`ယနေ့ဖျက်` - ယနေ့မှတ်တမ်းအားလုံး ဖျက်ရန်
`နောက်ဆုံးဖျက်` - နောက်ဆုံးစာရင်း ၁ စောင် ဖျက်ရန်
`စာရင်းအားလုံးဖျက်` - မှတ်တမ်းအားလုံး ဖျက်ရန်
`မှတ်တမ်းဖျက်+အမည်` - လူတစ်ဦးချင်းစီ၏ စာရင်းဖျက်ရန်
"""
    else:
        return """
🤖 *记账机器人使用指南*

📌 *记账格式：*
`+1000` - 入款1000元
`-1000` - 入款-1000元 (扣减款)
`备注+2000` - 带备注入款
`备注-2000` - 带备注减款
`下发50` - 下发50 USDT
`备注下发50` - 带备注下发50 USDT
`+0` - 查看今日汇总

📌 *管理命令：*
`上课` - 开启记账模式（开始全新记账）
`下课` - 关闭记账模式（锁定并结束本轮，但不清除历史数据）
`设置汇率 7.2` - 设置汇率
`设置操作人` - 设置操作人（回复某人消息后发送）
`查看操作员列表` - 查看操作人列表
`改语言` - 切换语言（中文/缅甸语）
`设置时间` - 设置时区
`显示U` - 显示USDT单位
`隐藏U` - 隐藏USDT单位

📌 *删除命令：*
`删今天` - 删除今日所有账单
`删最后` - 删除最后一笔账单
`全部清单` - 清空历史所有账单
`清单+备注` - 删除某人的账单 (例如: `清单+张三`)
"""

def get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, show_usdt, today_date, lang):
    unit = " USDT" if show_usdt == 1 else ""
    if lang == 'myanmar':
        income_title, expense_title, more_text, exchange_text = "📥 ငွေဝင်", "📤 ထုတ်ငွေ", "ကျန်ရှိ", "💰 လဲနှုန်း"
        total_income_text, total_expense_text, remaining_text = "📊 စုစုပေါင်း", "📊 ထုတ်ပြီး", "📊 ကျန်ငွေ"
    else:
        income_title, expense_title, more_text, exchange_text = "📥 入款", "📤 下发", "还有", "💰 汇率"
        total_income_text, total_expense_text, remaining_text = "📊 总入款", "📊 已下发", "📊 未下发"
        
    message = f"📊 实时课时账单汇总 ({today_date})\n━━━━━━━━━━━━━━━━━━━━\n\n"
    if income:
        message += f"{income_title}({len(income)} 笔):\n"
        for bill in income[:5]:
            remark, username, amount, usdt, ex_rate, ts = bill
            time_short = ts[5:16] if (ts and len(ts) > 11) else (ts or '')
            if remark:
                message += f"  【{remark}】{time_short}  {amount or 0:.0f} / {ex_rate or rate:.2f} = {usdt or 0:.2f}{unit}\n"
            else:
                message += f"  {time_short}  {amount or 0:.0f} / {ex_rate or rate:.2f} = {usdt or 0:.2f}{unit}\n"
        if len(income) > 5:
            message += f"  ... {more_text} {len(income)-5} 笔\n"
        message += "\n"
    else:
        message += f"{income_title}(0 笔):\n\n"
        
    if expense:
        message += f"{expense_title}({len(expense)} 笔):\n"
        for bill in expense[:5]:
            remark, username, usdt, ex_rate, ts = bill
            time_short = ts[5:16] if (ts and len(ts) > 11) else (ts or '')
            if remark:
                message += f"  【{remark}】{time_short}  {usdt or 0:.2f}{unit}\n"
            else:
                message += f"  {time_short}  {usdt or 0:.2f}{unit}\n"
        if len(expense) > 5:
            message += f"  ... {more_text} {len(expense)-5} 笔\n"
        message += "\n"
    else:
        message += f"{expense_title}(0 笔):\n\n"
        
    message += f"{exchange_text}：{rate:.2f}\n"
    message += f"{total_income_text}：{total_rmb:.0f} | {total_usdt:.2f}{unit}\n"
    message += f"{total_expense_text}：{expense_usdt:.2f}{unit}\n"
    message += f"{remaining_text}：{total_usdt - expense_usdt:.2f}{unit}"
    return message

async def show_full_bill(update: Update, gid):
    tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")
    
    income, expense, total_income, total_expense = get_class_bills_by_date(gid, today_date)
    rate = get_setting(gid, 'exchange_rate') or 7.2
    show_usdt = get_setting(gid, 'show_usdt') or 1
    lang = get_setting(gid, 'language') or 'chinese'
    total_rmb = total_income[0] or 0
    total_usdt = total_income[1] or 0
    expense_usdt = total_expense[0] or 0
    
    message = get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, show_usdt, today_date, lang)
    keyboard = [
        [InlineKeyboardButton("📊 查看完整账单 (Web)", url=f"{WEB_URL}?group_id={gid}")],
        [InlineKeyboardButton("📥 导出今日 CSV 表格", callback_data='export_csv')],
        [InlineKeyboardButton("📖 帮助 (Help)", callback_data='show_help')]
    ]
    await update.message.reply_text(message, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 🛡️ 核心大厅监听与商业拦截系统
# ==========================================

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    gid = update.effective_chat.id
    uid = update.effective_user.id
    username = update.effective_user.first_name
    tg_username = update.effective_user.username or "无用户名"
    chat_type = update.effective_chat.type
    current_token = context.bot.token
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    license_info = get_bot_license(current_token)
    
    # ----------------------------------------------------
    # 【商业拦截 - 流程 A】未绑定状态 -> 允许私聊自助锁定老板
    # ----------------------------------------------------
    if license_info['owner_id'] is None:
        if chat_type == "private":
            bind_bot_owner(current_token, uid, tg_username)
            await update.message.reply_text(
                f"🎉 **所有者自助绑定成功！**\n\n"
                f"👤 **管理员用户名**: @{tg_username}\n"
                f"🆔 **您的个人 ID**: `{uid}`\n\n"
                f"💡 您已成功锁定为此机器人的最高老板。请先在私聊中点击 /start 激活，随后即可拉入群内使用。\n"
                f"📅 初始赠送授权截止日期：`2026-06-15`"
            )
            return
        else:
            return # 没绑定前群里保持绝对安静

    # ----------------------------------------------------
    # 【商业拦截 - 流程 B】服务到期拦截 -> 群内装死，私聊弹窗
    # ----------------------------------------------------
    owner_id = license_info['owner_id']
    expire_time_str = license_info['expire_time']
    
    if today_str > expire_time_str:
        if uid == SYSTEM_ADMIN_ID:
            pass # 你作为最高管理员在调测，免拦截
        else:
            # 触发私聊推送
            if license_info['last_remind_date'] != today_str:
                try:
                    await context.bot.send_message(
                        chat_id=owner_id,
                        text=f"⚠️ **您的机器人服务已到期（到期时间：{expire_time_str}）**\n\n"
                             f"目前群内账单/计算等功能已暂停响应。请使用下方控制面板及时续费恢复：",
                        reply_markup=get_expired_keyboard(),
                        parse_mode="Markdown"
                    )
                    update_last_remind_date(current_token, today_str)
                except Exception as e:
                    logging.error(f"私聊通知客户老板失败: {e}")
            return # 🌟 核心硬要求：群内完全“装死”，不响应任何记账和管理指令

    # ----------------------------------------------------
    # 【商业拦截 - 流程 C】最高服务商（你）的绝对天神后台指令
    # ----------------------------------------------------
    if uid == SYSTEM_ADMIN_ID:
        if text.startswith("后台续费"):
            try:
                days = int(text.split()[1])
                conn = sqlite3.connect('bot_data.db')
                c = conn.cursor()
                start_date = datetime.strptime(max(expire_time_str, today_str), "%Y-%m-%d")
                new_date = (start_date + timedelta(days=days)).strftime("%Y-%m-%d")
                c.execute("UPDATE bot_licenses SET expire_time = ? WHERE bot_token = ?", (new_date, current_token))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"👑 尊贵的系统管理员：已成功为该机器人强制续费 {days} 天。\n📅 新到期时间：`{new_date}`")
            except Exception:
                await update.message.reply_text("格式错误，请输：后台续费 30")
            return
            
        elif text == "重置机器人":
            conn = sqlite3.connect('bot_data.db')
            c = conn.cursor()
            c.execute("UPDATE bot_licenses SET owner_id = NULL, owner_username = NULL WHERE bot_token = ?", (current_token,))
            conn.commit()
            conn.close()
            await update.message.reply_text("👑 尊贵的系统管理员：当前机器人的客户绑定已解除，恢复全新待绑定状态。")
            return

    # ----------------------------------------------------
    # 【原版功能 - 流程 D】完美执行你原汁原味的记账业务
    # ----------------------------------------------------
    if text in ['上课', 'အတန်းစက္ကူ']:
        if not can_use(gid, uid): return
        update_setting(gid, 'is_active', 1)
        msg = "🟢 记账模式已开启！全新课程开始，请发送数据记账。"
        if get_setting(gid, 'language') == 'myanmar':
            msg = "🟢 စာရင်းကိုင်မုဒ်ကို ဖွင့်လိုက်ပါပြီ။ စာရင်းအသစ် စတင်သွင်းနိုင်ပါပြီ。"
        await update.message.reply_text(msg)
        return

    if text in ['下课', 'အတန်းဆင်း']:
        if not can_use(gid, uid): return
        is_active = get_setting(gid, 'is_active') or 0
        if is_active == 0:
            await update.message.reply_text("❌ 当前本来就是下课状态。")
            return
        
        await update.message.reply_text("🛑 正在进行下课结算...")
        await show_full_bill(update, gid)
        
        tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_date = now.strftime("%Y-%m-%d")
        
        settle_today_bills(gid, today_date)
        update_setting(gid, 'is_active', 0)
        
        msg = f"🔴 下课成功！已为您锁定在线账单归档。历史记录完美保留，您可以随时打开网页选择日期查看过去的任意明细！"
        if get_setting(gid, 'language') == 'myanmar':
            msg = "🔴 အတန်းဆင်းခြင်း အောင်မြင်ပါသည်။ စာရင်းများကို သိမ်းဆည်းထားပြီးဖြစ်၍ Web တွင် ပြန်လည်ကြည့်ရှုနိုင်ပါသည်။"
        await update.message.reply_text(msg)
        return

    if text in ['查看操作员列表', 'အော်ပရေတာစာရင်း']:
        ops = json.loads(get_setting(gid, 'operators') or '[]')
        if not ops:
            await update.message.reply_text("📋 暂无操作人")
            return
        message = "📋 操作人列表:\n"
        for oid in ops:
            try:
                member = await context.bot.get_chat_member(gid, oid)
                message += f"  • {member.user.first_name}\n"
            except:
                message += f"  • ID: {oid}\n"
        await update.message.reply_text(message)
        return

    if text in ['设置操作人', 'အော်ပရေတာခန့်ရန်']:
        if not is_master(uid):
            await update.message.reply_text("❌ 只有机器人主人可以设置操作人")
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要设置为操作人的消息")
            return
        target = update.message.reply_to_message.from_user
        ops = json.loads(get_setting(gid, 'operators') or '[]')
        if target.id not in ops:
            ops.append(target.id)
            update_setting(gid, 'operators', json.dumps(ops))
            await update.message.reply_text(f"✅ 已设置 {target.first_name} 为操作人")
        else:
            await update.message.reply_text("该用户已经是操作人")
        return

    if text in ['改语言', 'ဘာသာစကား']:
        if not can_use(gid, uid): return
        current = get_setting(gid, 'language') or 'chinese'
        new_lang = 'myanmar' if current == 'chinese' else 'chinese'
        update_setting(gid, 'language', new_lang)
        msg = "✅ 已切换为中文" if new_lang == 'chinese' else "✅ မြန်မာဘာသာသို့ ပြောင်းလဲပြီးပါပြီ"
        await update.message.reply_text(msg)
        return

    if text in ['显示U', 'ယူပြရန်']:
        if not can_use(gid, uid): return
        update_setting(gid, 'show_usdt', 1)
        await update.message.reply_text("✅ 已开启 USDT 显示")
        return

    if text in ['隐藏U', 'ယူဝှက်ရန်']:
        if not can_use(gid, uid): return
        update_setting(gid, 'show_usdt', 0)
        await update.message.reply_text("🔕 已关闭 USDT 显示")
        return

    if text in ['删今天', 'ယနေ့ဖျက်']:
        if not can_use(gid, uid): return
        deleted = delete_today_bills(gid)
        await update.message.reply_text(f"✅ 已删除今日所有账单，共 {deleted} 条")
        return

    if text in ['删最后', 'နောက်ဆုံးဖျက်']:
        if not can_use(gid, uid): return
        deleted = delete_last_bill(gid)
        await update.message.reply_text("✅ 已删除最后一笔账单" if deleted else "📭 暂无账单可删")
        return

    if text in ['全部清单', 'စာရင်းအားလုံးဖျက်']:
        if not can_use(gid, uid): return
        deleted = delete_all_bills(gid)
        await update.message.reply_text(f"✅ 已清空全量总历史账单，共 {deleted} 条")
        return

    m_rate = re.match(r'^(?:设置汇率|ငွေလဲနှုန်း)\s+(\d+(?:\.\d+)?)$', text)
    if m_rate:
        if not can_use(gid, uid): return
        rate = float(m_rate.group(1))
        update_setting(gid, 'exchange_rate', rate)
        await update.message.reply_text(f"✅ 汇率已设为 {rate}")
        return

    m_tz = re.match(r'^(?:设置时间|အချိန်သတ်မှတ်)\s+([a-zA-Z]+)$', text)
    if m_tz:
        if not can_use(gid, uid): return
        tz_name = m_tz.group(1).lower()
        if tz_name in TIMEZONES:
            update_setting(gid, 'timezone', TIMEZONES[tz_name])
            await update.message.reply_text("✅ 时区修改成功")
        else:
            await update.message.reply_text("可用时区: china, myanmar, thailand")
        return

    m_del_user = re.match(r'^(?:清单\+|မှတ်တမ်းဖျက်\+)(.+)$', text)
    if m_del_user:
        if not can_use(gid, uid): return
        target_name = m_del_user.group(1).strip()
        deleted = delete_user_bills(gid, target_name)
        await update.message.reply_text(f"✅ 已清空【{target_name}】的账单共 {deleted} 条")
        return

    is_active = get_setting(gid, 'is_active') or 0
    if is_active == 0 or not can_use(gid, uid):
        return

    if text == '+0':
        await show_full_bill(update, gid)
        return

    m_exp = re.match(r'^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$', text)
    if m_exp:
        rem = m_exp.group(1).strip()
        amt = float(m_exp.group(2))
        add_bill(gid, uid, username, rem, amt, 'expense')
        await show_full_bill(update, gid)
        return

    m_inc = re.match(r'^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$', text)
    if m_inc:
        rem = m_inc.group(1).strip()
        sign = m_inc.group(2)
        amt = float(m_inc.group(3))
        if sign == '-': 
            amt = -amt
        custom_rate = float(m_inc.group(4)) if m_inc.group(4) else None
        ex_rate = custom_rate if custom_rate else (get_setting(gid, 'exchange_rate') or 7.2)
        
        add_bill(gid, uid, username, rem, amt, 'income', ex_rate)
        await show_full_bill(update, gid)
        return


# ==========================================
# 🖱️ 全网内联回调点击拦截器 (包含到期键盘响应)
# ==========================================

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    gid = update.effective_chat.id
    uid = update.effective_user.id
    data = query.data
    current_token = context.bot.token
    
    # 抓取当前机器人的授权数据
    license_info = get_bot_license(current_token)
    
    # ----------------------------------------------------
    # 触发商业到期控制键盘的专属事件
    # ----------------------------------------------------
    if data.startswith("btn_"):
        # 只有当前锁定的客户老板有资格点击这些授权面板
        if not license_info or uid != license_info['owner_id']:
            return
            
        if data == "btn_renew":
            base_price = 125.0  
            suffix = round(random.uniform(0.001, 0.099), 3) 
            final_amount = base_price + suffix
            
            # 精准对齐你的全功能账单输出
            invoice_text = (
                f"🧾 **订单已创建！请在 2 小时内**\n"
                f"支付 `{final_amount}` **USDT**\n"
                f"TRC-20地址：`{USDT_ADDRESS}`\n\n"
                f" • 注：地址为 **rjoCw** 结尾\n"
                f" • 请务必按指定金额和小数转账，否则无法自动化延期。\n"
                f" • 充值成功后，**3 分钟后**再次查看时间。\n"
                f" • 如充值有问题，请联系卖家 (10:00-0:00)"
            )
            await query.message.reply_text(invoice_text, parse_mode="Markdown")
            
        elif data == "btn_expire_time":
            await query.message.reply_text(f"📅 您当前机器人的服务截止日期为：`{license_info['expire_time']}`")
            
        elif data == "btn_docs":
            await query.message.reply_text("📖 **详细说明书**：\n[此处可以填入你的产品说明文档链接]")
            
        elif data in ["btn_trial", "btn_start", "btn_set_admin", "btn_set_operator", "btn_toggle_calc"]:
            await query.message.reply_text("❌ 该机器人服务已到期，各项高级设置功能已锁定，请先点击【🔄 自动续费】完成充值。")
        return

    # ----------------------------------------------------
    # 原版记账功能自带的内联按钮事件
    # ----------------------------------------------------
    if data == 'export_csv':
        tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_date = now.strftime("%Y-%m-%d")
        
        income, expense, _, _ = get_class_bills_by_date(gid, today_date)
        if not income and not expense:
            await query.message.reply_text("📭 今日暂无有效数据可以导出。")
            return
            
        f = io.StringIO()
        writer = csv.writer(f)
        writer.writerow(['类型', '备注说明', '提交时间', '金额(RMB)', '固定汇率', '等值数量(USDT)', '记账操作人'])
        
        for r in income:
            writer.writerow(['入款', r[0], r[5], r[2], r[4], r[3], r[1]])
        for r in expense:
            writer.writerow(['下发', r[0], r[4], '-', '-', r[2], r[1]])
            
        output = io.BytesIO(f.getvalue().encode('utf-8-sig'))
        output.name = f"账单明细_{today_date}.csv"
        await query.message.reply_document(document=output, filename=output.name, caption=f"📊 属于本群的今日 ({today_date}) 数据账单明细 CSV 表格已生成。")
        return

    if data == 'show_help':
        lang = get_setting(gid, 'language') or 'chinese'
        keyboard = [[InlineKeyboardButton("🔙 返回 (Back)", callback_data='back_to_main')]]
        await query.edit_message_text(get_help_text(lang), parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == 'back_to_main':
        rate = get_setting(gid, 'exchange_rate') or 7.2
        is_active = get_setting(gid, 'is_active') or 0
        status = "🟢 上课中" if is_active else "🔴 已下课"
        message = f"🤖 *记账机器人*\n\n📌 状态: {status}\n💰 汇率: 1 USDT = {rate:.2f} 元\n"
        keyboard = [
            [InlineKeyboardButton("📊 查看完整账单 (Web)", url=f"{WEB_URL}?group_id={gid}")],
            [InlineKeyboardButton("📥 导出今日 CSV 表格", callback_data='export_csv')],
            [InlineKeyboardButton("📖 帮助 (Help)", callback_data='show_help')]
        ]
        await query.edit_message_text(message, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id
    uid = update.effective_user.id
    chat_type = update.effective_chat.type
    lang = get_setting(gid, 'language') or 'chinese'
    
    # 1. 获取你原有的记账机器人使用指南文本
    guide_text = get_help_text(lang)
    
    # 2. 判断是否是客户“私聊”发送 /start
    if chat_type == "private":
        current_token = context.bot.token
        license_info = get_bot_license(current_token)
        
        # 追加一段私聊老板的提示，引导他看下方的按钮
        private_suffix = (
            "\n\n━━━━━━━━━━━━━━━━━━━━\n"
            "👑 **【老板私聊控制面板】**\n"
            "检测到您正在私聊管理机器人，请使用下方按钮进行**充值续费**、**开通试用**或查看状态。"
        )
        
        # 发送指南的同时，把你在代码中写好的 8 键精美菜单（get_expired_keyboard）挂上去
        await update.message.reply_text(
            text=guide_text + private_suffix, 
            parse_mode='Markdown', 
            reply_markup=get_expired_keyboard()  # 🌟 这里直接调用你原本就有的内联按钮函数
        )
    else:
        # 如果是在群聊里发 /start，就只显示普通的纯文字指南，不干扰群友
        await update.message.reply_text(guide_text, parse_mode='Markdown')

def run_web():
    flask_app.run(host='0.0.0.0', port=PORT)

# ==========================================
# 🚀 统一入口启动引擎
# ==========================================
def main():
    init_db()
    print("🤖 搭载全能商业授权闭环的超级记账机器人上电中...")
    threading.Thread(target=run_web, daemon=True).start()
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    
    # 统一分发内联按钮与普通文本拦截器
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
