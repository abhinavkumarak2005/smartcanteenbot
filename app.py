import os
import sys
import traceback
from flask import Flask, request, jsonify, send_file, url_for
from telebot import TeleBot, types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, Update
import threading
import requests
from PIL import Image, ImageDraw, ImageFont # Added PIL

# Global startup error capture
STARTUP_ERROR = None

try:
    from dotenv import load_dotenv
    from pathlib import Path
    
    # Load environment variables early
    BASE_DIR = Path(__file__).resolve().parent
    DOTENV_PATH = BASE_DIR / '.env'
    load_dotenv(dotenv_path=DOTENV_PATH)

    import db_manager
    import telebot
    from telebot.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, Update
    import qrcode
    import uuid
    import urllib.parse
    import json
    import time
    from datetime import datetime, timedelta
    import logging
    import razorpay
    import io
    import socket 
except Exception as e:
    STARTUP_ERROR = f"🔥 CRITICAL STARTUP ERROR:\n{traceback.format_exc()}"
    print(STARTUP_ERROR) # Print to Vercel logs


# Load environment variables
load_dotenv(dotenv_path=DOTENV_PATH)

# --- TELEGRAM & RAZORPAY SETUP ---
TOKEN = os.getenv('BOT_TOKEN')
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')
BOT_PUBLIC_URL = os.getenv('BOT_PUBLIC_URL')
# For Supabase Storage (QR Codes)
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')
SUPABASE_QR_BUCKET_URL = os.getenv('SUPABASE_QR_BUCKET_URL')

# Configuration for Webhook
RAZORPAY_WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', 'your_secret_webhook_key_default')

# Initialize TeleBot
try:
    bot = telebot.TeleBot(TOKEN, threaded=False) # Threaded=False for serverless safety
except Exception as e:
    print(f"❌ Error initializing TeleBot: {e}")
    # We don't exit here to allow Vercel to load the app object, but it will fail on request
    bot = None

# Initialize Razorpay Client
try:
    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
except Exception as e:
    print(f"❌ Error initializing Razorpay client: {e}")
    razorpay_client = None

# Initialize Supabase Client for Storage
try:
    from supabase import create_client
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
except Exception as e:
    print(f"❌ Error initializing Supabase client: {e}")
    supabase = None

# --- CONFIGURATION ---
ADMIN_CHAT_IDS = [int(num.strip()) for num in os.getenv('ADMIN_CHAT_IDS', '').split(',') if num.strip().isdigit()]
PAYEE_NAME = os.getenv('PAYEE_NAME', 'Canteen Staff')

# --- FLASK APP ENTRY POINT ---
app = Flask(__name__)

@app.route('/', methods=['GET'])
def index():
    print("🔹 V2.4 DEPLOYED: Fixes for Cart/Checkout/Price included")
    if STARTUP_ERROR:
        return f"<pre>{STARTUP_ERROR}</pre>", 500
    return "Telegram Canteen Bot is Running (Serverless) V2.4", 200

# --- HELPER FUNCTIONS ---
# (Keep existing helpers...)

# --- TELEGRAM HANDLERS (Manual call, no decorators needed for webhook) ---

# --- V2 HANDLERS ---

def handle_incoming_message(message, conn=None):
    """Manually handle incoming message."""
    try:
        print(f"🔹 Processing message from {message.chat.id}: {message.text}")
        incoming_msg = message.text.strip() if message.text else ''
        chat_id = message.chat.id
        telegram_id = chat_id # Use Telegram ID as primary key in V2

        # Check Admin
        if chat_id in ADMIN_CHAT_IDS:
            print(f"🔹 Routing to ADMIN flow for {chat_id}")
            handle_admin_commands(incoming_msg, chat_id, conn)
            return

        # --- GLOBAL CANCEL COMMAND ---
        if incoming_msg.lower() == 'cancel':
            db_manager.set_session_state(chat_id, 'initial', conn=conn)
            db_manager.set_session_data(chat_id, 'cart', [], conn=conn) # Clear cart
            
            # Check if registered
            if user:
                bot.send_message(chat_id, "❌ Action Cancelled.", reply_markup=main_menu_keyboard())
                db_manager.set_session_state(chat_id, 'menu', conn=conn)
            else:
                bot.send_message(chat_id, "❌ Registration Cancelled. Type /start to begin again.")
            return

        # --- WORKING HOURS CHECK ---
        open_time = db_manager.get_setting('open_time', '00:00', conn=conn)
        close_time = db_manager.get_setting('close_time', '23:59', conn=conn)
        
        now = datetime.now()
        current_time = now.strftime('%H:%M')
        
        # Simple string comparison works for HH:MM 24h format
        if not (open_time <= current_time <= close_time):
             bot.send_message(chat_id, f"🛑 **Canteen Closed**\nWe are open from {open_time} to {close_time}.")
             return

        # Check Registration Status (V2)
        user = db_manager.get_user(telegram_id, conn=conn)
        
        if not user:
            # Start Registration Flow
            handle_registration_flow(message, telegram_id, incoming_msg, conn)
        else:
            # User Valid -> Student Flow
            print(f"🔹 Routing to STUDENT flow for {user['name']}")
            handle_student_flow(incoming_msg, telegram_id, chat_id, user, conn)
            
    except Exception as e:
        print(f"❌ Handler Error: {e}")
        traceback.print_exc()

def handle_callback_query(call, conn=None):
    """Handle Inline Button Clicks."""
    try:
        print(f"🔹 Callback: {call.data} from {call.message.chat.id}")
        # Acknowledge immediately to prevent spinner stuck
        try: bot.answer_callback_query(call.id)
        except: pass
        
        chat_id = call.message.chat.id
        telegram_id = chat_id
        data = call.data
        msg_id = call.message.message_id
        
        # Admin Callbacks
        if chat_id in ADMIN_CHAT_IDS:
            if data == 'admin_report_today':
                date_str = datetime.now().strftime('%Y-%m-%d')
                bot.send_message(chat_id, "📊 Generating Today's Report...")
                
                orders = get_daily_report_data(date_str, conn)
                pdf_buffer = generate_pdf_report(orders, date_str)
                
                if pdf_buffer:
                    bot.send_document(chat_id, pdf_buffer, visible_file_name=f"Report_{date_str}.pdf", caption="Here is today's sales report 📄")
                else:
                    bot.send_message(chat_id, "❌ No data or error generating report.")
                return
            
            elif data == 'admin_report_custom':
                bot.send_message(chat_id, "📅 **Enter Date for Report**\nFormat: `YYYY-MM-DD`\nExample: `2024-01-25`", parse_mode='Markdown')
                db_manager.set_session_state(chat_id, 'admin_report_custom', conn=conn)
                return

            elif data == 'admin_menu':
                items = db_manager.get_menu(conn=conn)
                kb = types.InlineKeyboardMarkup()
                for i in items:
                    kb.add(types.InlineKeyboardButton(f"❌ Delete {i['name']}", callback_data=f"del_{i['id']}"))
                kb.add(types.InlineKeyboardButton("➕ Add New Item (Type 'add Name Price')", callback_data="admin_add_help"))
                kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_home"))
                bot.send_message(chat_id, "🍔 **Menu Management**\nTap to delete:", reply_markup=kb, parse_mode='Markdown')
                return

            elif data == 'admin_settings':
                # Show Settings Menu
                kb = types.InlineKeyboardMarkup()
                # working hours
                kb.add(types.InlineKeyboardButton("⏰ Set Open Time", callback_data="set_open_time"))
                kb.add(types.InlineKeyboardButton("🛑 Set Close Time", callback_data="set_close_time"))
                kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_home"))
                bot.send_message(chat_id, "⚙️ **Settings**\nConfigure bot operations:", reply_markup=kb, parse_mode='Markdown')
                return
            
            elif data in ['set_open_time', 'set_close_time']:
                mode = 'open' if data == 'set_open_time' else 'close'
                bot.send_message(chat_id, f"⏰ Enter **{mode.upper()} Time** (HH:MM 24hr format):\nExample: `09:00` or `18:00`", parse_mode='Markdown')
                db_manager.set_session_state(chat_id, f'admin_set_{mode}', conn=conn)
                return

            elif data.startswith('del_'):
                item_id = int(data.split('_')[1])
                db_manager.delete_menu_item(item_id, conn=conn)
                bot.answer_callback_query(call.id, "Item Deleted")
                bot.send_message(chat_id, "Item Deleted.")
                return

            elif data == 'admin_add_help':
                bot.answer_callback_query(call.id, "Cheatsheet")
                bot.send_message(chat_id, "💡 **To add an item:**\nType the command:\n`add Name Price`\nExample: `add Burger 50`", parse_mode='Markdown')
                return
            
            elif data.startswith('mark_delivered_'):
                order_id = int(data.split('_')[2])
                db_manager.update_order_status(order_id, 'delivered', conn=conn)
                
                # Update Button to "Delivered"
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("✅ Delivered", callback_data="noop"))
                
                try: 
                    bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=kb)
                except: pass
                
                # Notify User
                try:
                    order = db_manager.get_order(order_id, conn=conn)
                    user_id = order.get('user_id') or order.get('student_phone')
                    bot.send_message(user_id, f"✅ Order #{order.get('daily_token')} is ready/delivered! Enjoy.")
                except: pass
                
                return


            elif data == 'admin_home':
                handle_admin_commands("dashboard", chat_id, conn)
                return

        # Student Flow
        if data == 'menu':
            show_menu(chat_id, conn, message_to_edit=msg_id)

        elif data.startswith('add_'):
            # Step 1: User clicked Item -> Ask Quantity
            # data = add_{id}
            item_id = int(data.split('_')[1])
            ask_quantity(chat_id, item_id, msg_id, conn)

        elif data.startswith('qty_'):
            # Step 2: User clicked Quantity -> Add to Cart -> Show Mini Summary
            # data = qty_{qty}_{item_id}
            parts = data.split('_')
            qty = int(parts[1])
            item_id = int(parts[2])
            
            add_to_cart(chat_id, item_id, qty, conn)
            
            # Show "added" confirmation page
            show_mini_summary(chat_id, msg_id, start_checkout=False, conn=conn)

        elif data == 'view_cart':
            show_cart(chat_id, conn, message_to_edit=msg_id)

        elif data == 'clear_cart':
            db_manager.set_session_data(chat_id, 'cart', [], conn=conn)
            bot.answer_callback_query(call.id, "Cart Cleared")
            show_menu(chat_id, conn, message_to_edit=msg_id)

        elif data in ['checkout', 'confirm_order']:
            # Ask for Dining Option
            kb = types.InlineKeyboardMarkup()
            kb.row(types.InlineKeyboardButton("🍽️ Dine-in", callback_data="type_dinein"),
                   types.InlineKeyboardButton("📦 Parcel", callback_data="type_parcel"))
            kb.add(types.InlineKeyboardButton("🔙 Back to Cart", callback_data="view_cart"))
            try: bot.edit_message_text("🍽️ **Select Dining Option:**", chat_id, msg_id, reply_markup=kb, parse_mode='Markdown')
            except: bot.send_message(chat_id, "🍽️ **Select Dining Option:**", reply_markup=kb, parse_mode='Markdown')

        elif data in ['type_dinein', 'type_parcel']:
            # Handle Checkout with Type
            otype = 'Dine-in' if data == 'type_dinein' else 'Parcel'
            try: bot.edit_message_text(f"⏳ Generating Payment Link ({otype})...", chat_id, msg_id)
            except: pass
            handle_checkout(chat_id, conn, order_type=otype)
        
        try:
            bot.answer_callback_query(call.id)
        except: pass
        
    except Exception as e:
        print(f"❌ Callback Error: {e}")
        traceback.print_exc()

def handle_registration_flow(message, telegram_id, text, conn):
    """Handle new user registration."""
    # Check session state for registration step
    # We can store step in 'registration_data' or 'state'
    state = db_manager.get_session_state(telegram_id, conn=conn)
    print(f"🔹 Registration Flow: User {telegram_id} | State: {state} | Input: {text}")
    
    if text == '/start':
        # Reset registration if user sends /start
        db_manager.set_session_state(telegram_id, 'initial', conn=conn)
        state = 'initial'

    if state == 'initial':
        # Prompt Name
        bot.send_message(telegram_id, "👋 Welcome! It seems you are new here.\nPlease enter your **Full Name** to register:\n_(Type 'cancel' to stop)_", parse_mode='Markdown')
        db_manager.set_session_state(telegram_id, 'reg_name', conn=conn)
        
    elif state == 'reg_name':
        # Save Name, Prompt Phone
        # Robustness: Check if text is valid (not empty)
        if len(text) < 2:
             bot.send_message(telegram_id, "⚠️ Name too short. Please enter your Full Name:")
             return

        db_manager.set_session_data(telegram_id, 'registration_data', {'name': text}, conn=conn)
        bot.send_message(telegram_id, f"Nice to meet you, {text}! 🤝\nNow, please share your **Mobile Number** (or type it):", parse_mode='Markdown')
        db_manager.set_session_state(telegram_id, 'reg_phone', conn=conn)
        
    elif state == 'reg_phone':
        # Save Phone, Complete Registration
        reg_data = db_manager.get_session_data(telegram_id, 'registration_data', conn=conn)
        name = reg_data.get('name', 'Student')
        phone = text
        
        success = db_manager.register_user(telegram_id, name, phone, conn=conn)
        if success:
            bot.send_message(telegram_id, "✅ Registration Complete! You can now order food.")
            db_manager.set_session_state(telegram_id, 'menu', conn=conn)
            show_menu(telegram_id, conn)
        else:
            bot.send_message(telegram_id, "❌ Error saving profile. Please try again.")
            db_manager.set_session_state(telegram_id, 'initial', conn=conn)
    
    else:
        # Fallback for undefined states (Limbo Fix)
        # Verify if actually registered to avoid loops? 
        # No, 'user' check in main handler covers that.
        print(f"⚠️ User {telegram_id} in unknown state '{state}'. Resetting.")
        db_manager.set_session_state(telegram_id, 'initial', conn=conn)
        bot.send_message(telegram_id, "👋 Welcome! Let's get you registered.\nPlease enter your **Full Name**:\n_(Type 'cancel' to stop)_", parse_mode='Markdown')
        db_manager.set_session_state(telegram_id, 'reg_name', conn=conn)

def handle_student_flow(msg, telegram_id, chat_id, user, conn=None):
    """Handle registered student messages."""
    # Detect commands regardless of state
    if msg == '/start':
        # Clear cart on fresh start
        db_manager.set_session_data(chat_id, 'cart', [], conn=conn)
        show_menu(chat_id, conn)
        return
        
    if msg in ['menu', 'hi', 'hello']:
        show_menu(chat_id, conn)
        return

    # If text message comes in but we expect buttons, just show menu
    bot.send_message(chat_id, "Please use the buttons below:", reply_markup=main_menu_keyboard())

def show_menu(chat_id, conn, message_to_edit=None):
    """Display Menu."""
    try:
        items = db_manager.get_menu(conn=conn)
        if not items:
            bot.send_message(chat_id, "📋 Menu is currently empty.")
            return

        txt = "📋 *Today's Menu*\nSelect an item to order:\n_(Type 'cancel' to restart)_"
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        
        for item in items:
            btn_text = f"{item['name']}  -  ₹{item['price']}"
            keyboard.add(types.InlineKeyboardButton(btn_text, callback_data=f"add_{item['id']}"))
        
        keyboard.add(types.InlineKeyboardButton("🛒 View Cart", callback_data="view_cart"))
        
        if message_to_edit:
            try: bot.edit_message_text(txt, chat_id, message_to_edit, reply_markup=keyboard, parse_mode='Markdown')
            except: bot.send_message(chat_id, txt, reply_markup=keyboard, parse_mode='Markdown')
        else:
            bot.send_message(chat_id, txt, reply_markup=keyboard, parse_mode='Markdown')
    except Exception as e:
        print(f"Show menu error: {e}")
        bot.send_message(chat_id, "Error showing menu.")

def ask_quantity(chat_id, item_id, message_id, conn):
    """Show Quantity Buttons for selected item."""
    item = db_manager.get_menu_item(item_id, conn=conn)
    if not item: return

    txt = f"🍽 **{item['name']}**\nPrice: ₹{item['price']}\n\nSelect Quantity:"
    kb = types.InlineKeyboardMarkup(row_width=4)
    
    # Qty 1, 2, 3, 4
    btns = []
    for i in range(1, 5):
        btns.append(types.InlineKeyboardButton(str(i), callback_data=f"qty_{i}_{item_id}"))
    kb.add(*btns)
    
    # Custom Qty (For now just 5 and 10 to keep it simple without input states)
    kb.add(
        types.InlineKeyboardButton("5", callback_data=f"qty_5_{item_id}"),
        types.InlineKeyboardButton("10", callback_data=f"qty_10_{item_id}")
    )
    kb.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="menu"))
    
    bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb, parse_mode='Markdown')

def show_mini_summary(chat_id, message_id, start_checkout=False, conn=None):
    """Show 'Item Added' screen with item list (No Total)."""
    cart = db_manager.get_session_data(chat_id, 'cart', conn=conn)
    
    txt = "✅ **Added to Cart!**\n\n**Current Items:**\n"
    for i in cart:
         txt += f"• {i['name']} x{i['qty']} = ₹{i['price']*i['qty']}\n"
    
    # txt += "\nSelect an option:" # Cleanup newlines

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🍔 Add More Items", callback_data="menu"))
    kb.add(types.InlineKeyboardButton("💳 Checkout Now", callback_data="view_cart")) 
    
    bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb, parse_mode='Markdown')

def show_cart(chat_id, conn, message_to_edit=None):
    """Show Cart contents."""
    cart = db_manager.get_session_data(chat_id, 'cart', conn=conn)
    
    if not cart:
        txt = "🛒 Your cart is empty."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📋 Go to Menu", callback_data="menu"))
        if message_to_edit:
             bot.edit_message_text(txt, chat_id, message_to_edit, reply_markup=kb, parse_mode='Markdown')
        else:
             bot.send_message(chat_id, txt, reply_markup=kb, parse_mode='Markdown')
        return

    total = sum(i['price'] * i['qty'] for i in cart)
    txt = "🛒 *Your Cart*\n\n"
    for i in cart:
        txt += f"• {i['name']} x{i['qty']} = ₹{i['price']*i['qty']}\n"
    
    txt += f"\n**Total: ₹{total}**"
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("✅ Confirm & Pay", callback_data="checkout"))
    keyboard.add(types.InlineKeyboardButton("❌ Clear Cart", callback_data="clear_cart"))
    keyboard.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="menu"))
    
    if message_to_edit:
        bot.edit_message_text(txt, chat_id, message_to_edit, reply_markup=keyboard, parse_mode='Markdown')
    else:
        bot.send_message(chat_id, txt, reply_markup=keyboard, parse_mode='Markdown')

def add_to_cart(chat_id, item_id, qty, conn):
    """Add item to persistent cart."""
    cart = db_manager.get_session_data(chat_id, 'cart', conn=conn) or []
    item = db_manager.get_menu_item(item_id, conn=conn)
    
    if not item: return

    # Check if item in cart
    found = False
    for i in cart:
        if i['id'] == item_id:
            i['qty'] += qty
            found = True
            break
    
    if not found:
        cart.append({'id': item['id'], 'name': item['name'], 'price': item['price'], 'qty': qty})
        
    db_manager.set_session_data(chat_id, 'cart', cart, conn=conn)
    
    # Optional: Pop-up notification
    # bot.answer_callback_query(...) handled in dispatcher



def handle_checkout(chat_id, conn, order_type='Dine-in'):
    """Create order and generate payment link."""
    cart = db_manager.get_session_data(chat_id, 'cart', conn=conn)
    if not cart: return
    
    total = sum(i['price'] * i['qty'] for i in cart)
    user = db_manager.get_user(chat_id, conn=conn)
    
    # Create Order
    order_id = db_manager.create_order(user['phone_number'], cart, total, user_id=chat_id, conn=conn)
    
    if order_id:
        # Pass notes to Razorpay (requires updating generate func if not kwargs ready)
        # Assuming generate_razorpay_payment_link takes **kwargs or notes arg. 
        # I will update the definition of generate_razorpay_payment_link next.
        links, _ = generate_razorpay_payment_link(order_id, total, user['phone_number'], notes={'order_type': order_type})
        
        if links:
             db_manager.update_order_status(order_id, 'payment_pending', conn=conn)
             
             # Keyboard with Pay Button
             payment_url = links.get('razorpay_link')
             
             try:
                 qr = qrcode.QRCode(box_size=10, border=4)
                 qr.add_data(payment_url)
                 qr.make(fit=True)
                 img = qr.make_image(fill_color="black", back_color="white")
                 
                 bio = io.BytesIO()
                 img.save(bio, 'PNG')
                 bio.seek(0)
                 
                 kb = types.InlineKeyboardMarkup()
                 kb.add(types.InlineKeyboardButton("💳 Pay Now (Click)", url=payment_url))
                 
                 caption = f"✅ **Order Created! (ID: {order_id})**\n🍱 Type: **{order_type}**\nAmount: ₹{total}\n\nScan this QR to Pay or Click below:"
                 bot.send_photo(chat_id, bio, caption=caption, reply_markup=kb, parse_mode='Markdown')
             except Exception as qr_err:
                 print(f"QR Gen Error: {qr_err}")
                 # Fallback
                 kb = types.InlineKeyboardMarkup()
                 kb.add(types.InlineKeyboardButton("💳 Pay Now", url=payment_url))
                 bot.send_message(chat_id, f"✅ Order Created! ({order_type})\nAmount: ₹{total}\n\nTap below to pay:", reply_markup=kb)

             # Clear Cart
             db_manager.set_session_data(chat_id, 'cart', [], conn=conn)
        else:
             bot.send_message(chat_id, "❌ Error: Payment link generation failed.")
    else:
        bot.send_message(chat_id, "❌ Error creating order (DB).")
def main_menu_keyboard():
    k = types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("📋 View Menu", callback_data="menu"))
    return k

def process_order(chat_id, conn):
    pass # Replaced by handle_checkout


# --- TELEGRAM WEBHOOK (Moved to bottom to see handlers) ---
@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    """Endpoint for Telegram updates."""
    if not bot:
        return 'Bot not initialized', 500
        
    conn = None # Initialize conn
    try:
        json_string = request.get_data().decode('utf-8')
        print(f"🔹 Webhook received: {json_string}") # DEBUG LOG
        update = Update.de_json(json_string)
        
        # Verify bot token matches (optional but good for debugging)
        if not bot.token == TOKEN:
             print("⚠️ Bot token mismatch in memory!")

        # Process synchronously - MANUAL ROUTING
        if update.message:
            # Create ONE connection for the whole request
            conn = db_manager.create_connection()
            if not conn:
                print("❌ Failed to create DB connection in webhook")
            
            handle_incoming_message(update.message, conn=conn)
            
        elif update.callback_query:
            # Handle Button Clicks
            conn = db_manager.create_connection() # Reuse logic for separate update types
            handle_callback_query(update.callback_query, conn=conn)
            
        else:
            print("🔹 Update has no message/callback content")

        return 'OK', 200
    except Exception as e:
        print(f"❌ Telegram webhook error: {e}")
        traceback.print_exc()
        return 'Error', 500
    finally:
        # Close the shared connection
        if conn:
            conn.close()
            print("🔒 DB Connection closed.")

import psycopg2 # Add this import for debugging

# ... (imports)

@app.route('/init_db', methods=['GET'])
def init_db_route():
    """Initialize database tables manually."""
    # Debug: Try to resolve DNS first to show user
    db_url = os.getenv('SUPABASE_DB_URL', 'NOT_SET')
    
    debug_info = []
    try:
        from urllib.parse import urlparse
        hostname = urlparse(db_url).hostname
        debug_info.append(f"Target Host: {hostname}") # SHOW THIS TO USER
        ip = socket.gethostbyname(hostname) 
        debug_info.append(f"DNS IPv4: {ip}")
    except Exception as e:
        debug_info.append(f"DNS Error: {e}")

    try:
        # Use our robust db_manager connection
        conn = db_manager.create_connection()
        if conn:
            conn.close()
            # If connection works, proceed to create tables
            success = db_manager.create_tables()
            if success:
                # Add default items
                db_manager.add_default_menu_items()
                return f"✅ Database initialized successfully! <br>Debug Info: {', '.join(debug_info)}", 200
            else:
                return f"❌ Tables creation failed (SQL Error). Check logs for details.<br>Info: {', '.join(debug_info)}", 500
        else:
             return f"❌ Connection Failed even with IPv4 fix. <br>Info: {', '.join(debug_info)}", 500
             
    except Exception as e:
        return f"❌ Critical Error: {e} <br>Info: {', '.join(debug_info)}", 500

# --- RAZORPAY WEBHOOK ---
@app.route('/razorpay/webhook', methods=['POST'])
def handle_razorpay_webhook():
    """Handles payment successful notifications from Razorpay."""
    if request.method == 'POST':
        try:
            # 1. Verify the webhook signature
            signature = request.headers.get('X-Razorpay-Signature')
            raw_payload = request.data.decode('utf-8')

            try:
                razorpay_client.utility.verify_webhook_signature(raw_payload, signature, RAZORPAY_WEBHOOK_SECRET)
                print("✅ Razorpay webhook signature verified.")
            except Exception as e:
                print(f"❌ Webhook verification failed: {e}")
                return jsonify({'status': 'invalid signature'}), 400

            payload = json.loads(raw_payload)

            event_type = payload.get('event')
            
            if event_type in ['payment.captured', 'payment_link.paid']:
                current_order_id = None
                order_details = None
                
                # STRATEGY 1: Use Reference ID from Payment Link Event
                if event_type == 'payment_link.paid':
                    plink_entity = payload['payload']['payment_link']['entity']
                    ref_id = plink_entity.get('reference_id') 
                    if ref_id and str(ref_id).isdigit():
                        current_order_id = int(ref_id)
                        print(f"🔹 Resolved via Link Reference: {current_order_id}")
                
                # STRATEGY 2: Parse Payment Description (for payment.captured)
                elif event_type == 'payment.captured':
                    payment_entity = payload['payload']['payment']['entity']
                    description = payment_entity.get('description', '')
                    notes = payment_entity.get('notes', {})
                    print(f"🔹 Webhook Description: {description}")
                    
                    # 2a. Description
                    if description and '#' in description:
                        try:
                            # Handle "Canteen Order #16"
                            current_order_id = int(description.split('#')[1].strip().split()[0]) 
                            print(f"🔹 Extracted Order ID: {current_order_id}")
                        except: pass
                    
                    # 2b. Notes fallback (if Razorpay copied it)
                    if not current_order_id and 'reference_id' in notes:
                        try:
                            current_order_id = int(notes['reference_id'])
                            print(f"🔹 Extracted ID from Notes: {current_order_id}")
                        except: pass

                # STRATEGY 3: Lookup by Payment Link ID (Common for both)
                if not current_order_id:
                    # Try to find link ID in payload
                    plink_id = None
                    if event_type == 'payment.captured':
                        plink_id = payload['payload']['payment']['entity'].get('payment_link_id')
                    elif event_type == 'payment_link.paid':
                        plink_id = payload['payload']['payment_link']['entity'].get('id')
                    
                    if plink_id:
                         print(f"🔹 Lookup by Payment Link ID: {plink_id}")
                         order_details = db_manager.get_order_by_razorpay_order_id(plink_id)
                         if order_details: current_order_id = order_details['id']

                # FINAL PROCESSING
                if current_order_id and not order_details:
                     try: order_details = db_manager.get_order(current_order_id)
                     except: order_details = db_manager.get_order_details(current_order_id)

                if order_details:
                    print(f"🔹 Order Found for Processing: {order_details['id']} ({order_details['status']})")
                    if order_details['status'] == 'payment_pending':
                        
                        # 1. Update DB to Paid
                        db_manager.update_order_status(current_order_id, 'paid')
                        
                        # 2. Get Data for Token
                        items_data = db_manager.parse_order_items(order_details['items'])
                        token_num = order_details.get('daily_token', 0)
                        total_amt = order_details['total_amount']
                        student_chat_id = order_details.get('user_id') or order_details['student_phone']

                        # 3. Generate Link and QR
                        token_link = f"{request.host_url}token/{current_order_id}"
                        
                        try:
                            # Generate QR for the Link
                            qr = qrcode.QRCode(box_size=10, border=4)
                            qr.add_data(token_link)
                            qr.make(fit=True)
                            qr_img = qr.make_image(fill_color="black", back_color="white")
                            
                            bio = io.BytesIO()
                            qr_img.save(bio, 'PNG')
                            bio.seek(0)
                        
                            caption = (
                                f"🎉 **Payment Successful!**\n\n"
                                f"🔑 **Token #{token_num}**\n"
                                f"Scan or Click below to view your Digital Token (Valid for today only):\n"
                                f"{url_for('view_token', order_id=current_order_id, _external=True)}"
                            )
                            
                            bot.send_photo(student_chat_id, bio, caption=caption, parse_mode='Markdown')
                            
                        except Exception as qr_err:
                            print(f"Token Link QR Error: {qr_err}")
                            bot.send_message(student_chat_id, f"🎉 Paid! Token #{token_num}. View here: {token_link}")
                            
                        send_admin_notification(order_details, f"Token #{token_num}")

                        print(f"✅ Order {current_order_id} processed.")

            return jsonify({'status': 'success'}), 200

        except Exception as e:
            print(f"❌ Error processing Razorpay webhook: {e}")
            traceback.print_exc()
            return jsonify({'status': 'error'}), 500

    return jsonify({'status': 'invalid method'}), 405

@app.route('/payment_success', methods=['GET'])
def handle_razorpay_success_redirect():
    # Try different params Razorpay might send
    ref = request.args.get('razorpay_payment_link_reference_id') # Our Order ID
    if not ref:
        ref = request.args.get('razorpay_order_id')
    if not ref:
        ref = request.args.get('razorpay_payment_id')
        
    return f"<h1>Payment Successful! 🎉</h1><p>You can close this window.</p><p>Please check Telegram for your Token receipt (Ref: {ref if ref else 'Processed'}).</p>"

@app.route('/token/<order_id>', methods=['GET'])
def view_token(order_id):
    """View Digital Token (Self-Destructing) with Client-Side Download."""
    try:
        order = db_manager.get_order(order_id)
    except:
        order = db_manager.get_order_details(order_id)
        
    if not order: return "<h1>❌ Invalid Token</h1>", 404
    
    # Expiry Check
    try:
        created_at = order.get('created_at')
        if isinstance(created_at, str): 
             created_at = datetime.strptime(created_at.split('.')[0], "%Y-%m-%d %H:%M:%S")
        if created_at.date() != datetime.now().date():
            return "<h1>⏳ Token Link Expired</h1><p>This link is only valid for the day of purchase.</p>", 410
    except: pass

    # Format Data
    date_str = created_at.strftime('%b %d')
    token_display = f"{created_at.strftime('%b%d').upper()}-{order.get('daily_token')}"
    
    status_color = "#27ae60" if order['status'] == 'paid' else "#c0392b"
    status_text = "VALID" if order['status'] == 'paid' else order['status'].upper()
    
    # Items HTML
    try:
        items = json.loads(order['items']) if isinstance(order['items'], str) else order['items']
        items_html = "".join([f"<li><span>{i['name']}</span> <span>x{i['qty']}</span></li>" for i in items])
    except: items_html = "<li>Error parsing items</li>"
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{token_display}</title>
        <script src="https://html2canvas.hertzen.com/dist/html2canvas.min.js"></script>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #eef2f5; padding: 20px; text-align: center; color: #333; }}
            .container {{ max-width: 420px; margin: auto; }}
            .card {{ background: white; border-radius: 20px; box-shadow: 0 15px 35px rgba(0,0,0,0.15); overflow: hidden; position: relative; }}
            .header {{ background: #4a235a; color: white; padding: 25px 20px; }}
            .header h2 {{ margin: 0; font-size: 0.9em; opacity: 0.9; text-transform: uppercase; letter-spacing: 1.5px; }}
            .header h1 {{ margin: 10px 0 0; font-size: 2.5em; font-weight: 800; letter-spacing: 1px; }}
            
            .content {{ padding: 20px 25px; text-align: left; }}
            .status-wrapper {{ text-align: center; margin-top: -35px; margin-bottom: 20px; }}
            .status-badge {{ background: #d4edda; color: #155724; padding: 8px 25px; border-radius: 50px; font-weight: bold; border: 3px solid white; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }}
            
            p {{ margin: 10px 0; font-size: 1.05em; display: flex; justify-content: space-between; }}
            .label {{ font-weight: 600; color: #555; }}
            
            hr {{ border: 0; border-top: 1px dashed #ddd; margin: 20px 0; }}
            
            ul {{ list-style: none; padding: 0; margin: 0; }}
            li {{ display: flex; justify-content: space-between; padding: 8px 0; font-size: 1em; }}
            
            .total {{ display: flex; justify-content: space-between; font-weight: 800; font-size: 1.3em; margin-top: 20px; color: #4a235a; }}
            
            .footer {{ padding: 20px; background: white; border-top: 1px solid #f0f0f0; }}
            .btn {{ cursor: pointer; display: block; width: 100%; border: none; padding: 16px; background: #4a235a; color: white; border-radius: 12px; font-weight: bold; font-size: 1.1em; transition: transform 0.1s; text-decoration: none; }}
            .btn:active {{ transform: scale(0.98); }}
            .note {{ font-size: 0.8em; color: #888; margin-top: 12px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card" id="tokenCard">
                <div class="header">
                    <h2>Token</h2>
                    <h1>{token_display}</h1>
                </div>
                <div class="content">
                    <div class="status-wrapper"><span class="status-badge">{status_text}</span></div>
                    
                    <p><span class="label">Order ID</span> <span>{order_id}</span></p>
                    <p><span class="label">Date</span> <span>{date_str}</span></p>
                    
                    <hr>
                    <div style="font-weight: 600; color: #777; font-size: 0.9em; margin-bottom: 10px;">ITEMS</div>
                    <ul>{items_html}</ul>
                    
                    <hr>
                    <div class="total">
                        <span>Total</span>
                        <span>₹{order['total_amount']}</span>
                    </div>
                </div>
                
                <!-- Footer for visual balance, but buttons outside for screenshot -->
                <div style="height: 20px;"></div>
            </div>
            
            <div class="footer" style="background: transparent; border: none; margin-top: 20px;">
                <button onclick="downloadToken()" class="btn">📸 Save to Gallery</button>
                <div class="note">Link expires at midnight</div>
            </div>
        </div>

        <script>
            function downloadToken() {{
                var btn = document.querySelector('button');
                btn.innerText = "Saving...";
                
                html2canvas(document.getElementById("tokenCard"), {{
                    scale: 3,
                    useCORS: true,
                    backgroundColor: null 
                }}).then(canvas => {{
                    var link = document.createElement('a');
                    link.download = 'Token-{token_display}.png';
                    link.href = canvas.toDataURL("image/png");
                    link.click();
                    btn.innerText = "📸 Save to Gallery";
                }});
            }}
        </script>
    </body>
    </html>
    """
    return html

# Removed server-side download route since we handle it on client now
# Keeping webhooks intact



# --- PAYMENT HELPER FUNCTIONS ---

def generate_razorpay_payment_link(order_id, amount, phone_number, notes=None):
    """Generates a Razorpay payment link."""
    try:
        if not RAZORPAY_KEY_ID: return None, None
        
        amount_paisa = int(amount * 100)
        
        # Ensure phone is valid E.164 or at least 10 digits
        contact_str = str(phone_number).replace('+', '') 
        if len(contact_str) < 10: contact_str = "9999999999" # Fallback dummy if invalid
        
        # Create Payment Link
        rzp_link = razorpay_client.payment_link.create({
            "amount": amount_paisa,
            "currency": "INR",
            "accept_partial": False,
            "expire_by": int((datetime.now() + timedelta(minutes=20)).timestamp()),
            "reference_id": str(order_id),
            "description": f"Canteen Order #{order_id}",
            "customer": {
                "name": PAYEE_NAME,
                "contact": contact_str, 
            },
            "notify": {"sms": False, "email": False},
            "callback_url": f"{BOT_PUBLIC_URL}/payment_success",
            "callback_method": "get",
            "notes": notes if notes else {}
        })

        payment_url = rzp_link['short_url']
        expiration_time = datetime.now() + timedelta(minutes=15)
        
        # We need the rzp_order_id, but payment links create orders internally or differently.
        # For simplicity, we'll store the link ID or reference.
        # Ideally, we create an Order first, then a Link, but `paymentLink.create` is simpler.
        # We will capture the `id` of the payment link as the ref, 
        # BUT webhook returns `order_id` if we used standard checkout. 
        # `paymentLink` creates a `plink_...` ID.
        # The webhook 'payment.captured' payload contains 'order_id' ONLY if created via Orders API.
        # If created via Payment Link, we might need to match via `payment_link_id` or `reference_id` in webhook entity.
        # FIX: The previous code assumed order_id matching. 
        # Let's ensure we store the correct reference. 
        # For this refactor, we'll rely on `reference_id` which we set to our internal `order_id`.
        # However, db_manager expects `razorpay_order_id`.
        # We will store the `plink_ID` for now.
        
        # UPDATE: Since we can't easily change the webhook logic without user testing, 
        # we will assume the webhook will look up the order using the `razorpay_order_id` column.
        # `rzp_link['id']` is `plink_...`. We'll save that.
        
        db_manager.update_order_razorpay_id(order_id, rzp_link['id'])

        return {'razorpay_link': payment_url}, expiration_time.strftime('%Y-%m-%d %H:%M:%S')

    except Exception as e:
        print(f"❌ Error generating link: {e}")
        traceback.print_exc()
        return None, None

def generate_pickup_qr_code(order_id, student_phone, items_summary):
    """Generate pickup QR code and upload to Supabase."""
    try:
        pickup_data = {
            'order_id': order_id,
            'phone': student_phone,
            'verification_code': f"{order_id}{datetime.now().strftime('%H%M')}"
        }
        pickup_json = json.dumps(pickup_data)
        
        # Generate QR
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(pickup_json)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="darkgreen", back_color="white")
        
        # Save to Buffer
        img_buffer = io.BytesIO()
        qr_img.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        
        # Upload to Supabase
        filename = f"pickup_{order_id}_{uuid.uuid4().hex[:8]}.png"
        if supabase:
            supabase.storage.from_("qr-codes").upload(
                path=filename,
                file=img_buffer.getvalue(),
                file_options={"content-type": "image/png"}
            )
            # Public URL
            if SUPABASE_QR_BUCKET_URL:
                 public_url = urllib.parse.urljoin(SUPABASE_QR_BUCKET_URL + '/', filename)
            else:
                 # Fallback if bucket URL not set (try to construct)
                 public_url = f"{SUPABASE_URL}/storage/v1/object/public/qr-codes/{filename}"
            
            return public_url, pickup_data['verification_code']
        else:
            return None, pickup_data['verification_code']

    except Exception as e:
        print(f"❌ Error generating pickup QR: {e}")
        return None, None

def generate_token_image(token_number, order_id, items, total, student_name):
    """Generate a digital token receipt image using custom template."""
    try:
        # Load Template
        template_path = os.path.join(BASE_DIR, 'token_template.png')
        if os.path.exists(template_path):
            img = Image.open(template_path).convert('RGB')
        else:
            img = Image.new('RGB', (791, 1024), (255, 255, 255))
            
        draw = ImageDraw.Draw(img)
        # width, height = 791, 1024

        text_color = (60, 20, 80) # Dark Purple
        green_color = (0, 150, 0)

        # Fonts
        font_path = os.path.join(BASE_DIR, 'Roboto-Bold.ttf')
        try:
            # Use larger fonts for High-Res template
            font_header = ImageFont.truetype(font_path, 60)
            font_text = ImageFont.truetype(font_path, 28)
            font_small = ImageFont.truetype(font_path, 24)
        except:
            font_header = ImageFont.load_default()
            font_text = ImageFont.load_default()
            font_small = ImageFont.load_default()

        # 1. Token Number (Header) - Centered
        date_prefix = datetime.now().strftime('%b%d').upper()
        token_str = f"{date_prefix}-{token_number}"
        
        try:
             w = draw.textlength(token_str, font=font_header)
             x_header = (791 - w) // 2
        except: x_header = 300
        
        draw.text((x_header, 210), token_str, fill=text_color, font=font_header)
        
        # 2. Left Column Details
        # Moved RIGHT to x=360 to avoid overlapping labels
        x_val = 360 
        y_start = 325 
        gap = 45
        
        draw.text((x_val, y_start), str(order_id), fill=text_color, font=font_text)
        
        # Truncate long names
        s_name = str(student_name)
        if len(s_name) > 15: s_name = s_name[:12] + "..."
        draw.text((x_val, y_start + gap), s_name, fill=text_color, font=font_text)
        
        draw.text((x_val, y_start + gap*2), datetime.now().strftime('%d-%m-%y'), fill=text_color, font=font_text)
        draw.text((x_val, y_start + gap*3), "VERIFIED", fill=green_color, font=font_text)

        # 3. Right Column (Items/Total)
        x_right = 530
        y_item = 325
        
        display_items = items[:4]
        for item in display_items:
            line = f"{item['name'][:10]} x{item['qty']}"
            draw.text((x_right, y_item), line, fill=text_color, font=font_small)
            y_item += 28
            
        # Total
        draw.text((610, 475), f"Rs. {total}", fill=text_color, font=font_text) 

        # 4. QR Code
        verify_url = f"{BOT_PUBLIC_URL}/verify_token?order_id={order_id}"
        
        qr = qrcode.QRCode(box_size=10, border=0)
        qr.add_data(verify_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        
        qr_size = 350
        qr_img = qr_img.resize((qr_size, qr_size))
        
        # Center in box (Width 791. QR 350. (791-350)/2 = 220)
        # y start = 560
        img.paste(qr_img, (220, 560))
        
        # Scan Text
        try:
             msg = "Scan to Verify"
             w = draw.textlength(msg, font=font_text)
             x_msg = (791 - w) // 2
        except: x_msg = 300
        
        draw.text((x_msg, 930), "Scan to Verify", fill=text_color, font=font_text)

        img_buffer = io.BytesIO()
        img.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        return img_buffer

    except Exception as e:
        print(f"❌ Error generating token image: {e}")
        return None

def create_payment_keyboard(payment_links, order_id):
    try:
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.row(InlineKeyboardButton("💳 Pay Securely with Razorpay", url=payment_links['razorpay_link']))
        return keyboard
    except:
        return None

# --- ADMIN DASHBOARD & REPORTS (V2) ---
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from psycopg2.extras import DictCursor

def handle_admin_commands(msg, chat_id, conn=None):
    """Admin Logic"""
    
    # 0. Global Reset for Admins
    if msg.lower() in ['/start', 'cancel', 'dashboard']:
         db_manager.set_session_state(chat_id, 'initial', conn=conn)
         # Fallthrough to show dashboard below (since state is now initial)
         # We want to skip the state processing logic below
    
    # 1. Check for State-Based Inputs (Custom Report / Settings)
    state = db_manager.get_session_state(chat_id, conn=conn)
    
    # Ensure we don't process /start as a date even if state is somehow stuck
    if state == 'admin_report_custom' and msg.lower() not in ['/start', 'cancel', 'dashboard']:
        # msg is the Date
        try:
            date_obj = datetime.strptime(msg, '%Y-%m-%d')
            date_str = msg
            orders = get_daily_report_data(date_str, conn)
            if not orders:
                bot.send_message(chat_id, f"❌ No data found for {date_str}.")
            else:
                 pdf = generate_pdf_report(orders, date_str)
                 if pdf: bot.send_document(chat_id, pdf, visible_file_name=f"Report_{date_str}.pdf", caption=f"Report for {date_str}")
                 else: bot.send_message(chat_id, "Error generating.")
                 
            db_manager.set_session_state(chat_id, 'initial', conn=conn)
            return
        except ValueError:
            bot.send_message(chat_id, "❌ Invalid Format. Use YYYY-MM-DD (e.g., 2024-01-30). Try again:")
            return

    elif state == 'admin_set_open':
        db_manager.set_setting('open_time', msg, conn=conn)
        bot.send_message(chat_id, f"✅ Opening time set to {msg}")
        db_manager.set_session_state(chat_id, 'initial', conn=conn)
        return

    elif state == 'admin_set_close':
        db_manager.set_setting('close_time', msg, conn=conn)
        bot.send_message(chat_id, f"✅ Closing time set to {msg}")
        db_manager.set_session_state(chat_id, 'initial', conn=conn)
        return


    # 2. Text Commands
    if msg.lower().startswith("add "):
        # add Name Price
        parts = msg.split(' ')
        if len(parts) >= 3:
            price = parts[-1]
            name = " ".join(parts[1:-1])
            try:
                res = db_manager.add_menu_item(name, float(price))
                bot.send_message(chat_id, res)
            except:
                bot.send_message(chat_id, "❌ Error. Use: `add Name Price`")
        return
        
    elif msg.lower().startswith("delete "):
         # Fallback for manual delete ID
         try:
             item_id = int(msg.split(' ')[1])
             res = db_manager.delete_menu_item(item_id) # Using conn inside? Need to verify db_manager uses passed conn if provided
             # Our db_manager helpers create new conn currently if not passed.
             # We should update db_manager to accept conn or just let it make one.
             # Current helper `delete_menu_item(item_id)` does not accept conn arg in definition?
             # Checking definition... yes it doesn't take conn.
             # That's fine for low volume commands.
             bot.send_message(chat_id, res)
         except:
             bot.send_message(chat_id, "❌ Invalid ID.")
         return


    # Send Dashboard
    txt = "👮‍♂️ **Admin Dashboard**\nSelect an action:"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 Today's Report", callback_data="admin_report_today"),
        types.InlineKeyboardButton("📅 Custom Report", callback_data="admin_report_custom"),
        types.InlineKeyboardButton("🍔 Manage Menu", callback_data="admin_menu"),
        types.InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")
    )
    bot.send_message(chat_id, txt, reply_markup=kb, parse_mode='Markdown')

def get_daily_report_data(date_str, conn):
    """Fetch paid orders for a specific date with user names."""
    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('''
                SELECT o.*, u.name as user_name
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.telegram_id
                WHERE o.status = 'paid' 
                AND o.created_at::date = %s
                ORDER BY o.created_at ASC
            ''', (date_str,))
            orders = [dict(row) for row in cursor.fetchall()]
        return orders
    except Exception as e:
        print(f"Error fetching report: {e}")
        return []

def generate_pdf_report(orders, date_str):
    """Generate PDF report for the day."""
    try:
        buffer = io.BytesIO()
        p = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        
        # Header
        p.setFont("Helvetica-Bold", 20)
        p.drawString(50, height - 50, f"Canteen Sales Report - {date_str}")
        
        p.setFont("Helvetica", 12)
        p.drawString(50, height - 80, f"Generated at: {datetime.now().strftime('%H:%M:%S')}")
        
        # Table Header
        y = height - 120
        p.setFont("Helvetica-Bold", 10)
        p.drawString(40, y, "Token")     # New
        p.drawString(90, y, "Customer")
        p.drawString(190, y, "Phone")    # New
        p.drawString(290, y, "Items")
        p.drawString(500, y, "Amt")
        p.line(40, y-5, 550, y-5)
        y -= 25
        
        total_revenue = 0
        p.setFont("Helvetica", 10)
        
        for order in orders:
            if y < 50: # New Page
                p.showPage()
                y = height - 50
                
            # Token
            try:
                token_val = f"{order['created_at'].strftime('%b%d').upper()}-{order.get('daily_token', '?')}"
            except: token_val = str(order['id'])
            p.drawString(40, y, token_val)
            
            # Name
            c_name = order.get('user_name') or "Unknown"
            p.drawString(90, y, c_name[:15])
            
            # Phone
            phone = str(order.get('student_phone', ''))
            p.drawString(190, y, phone)
            
            # Items
            items = db_manager.parse_order_items(order['items'])
            item_str = ", ".join([f"{i['name']}x{i['qty']}" for i in items])
            if len(item_str) > 35: item_str = item_str[:32] + "..."
            p.drawString(290, y, item_str)
            
            # Amount
            p.drawString(500, y, f"{order['total_amount']}")
            
            total_revenue += order['total_amount']
            y -= 20
            
        p.line(40, y+10, 550, y+10)
        p.setFont("Helvetica-Bold", 12)
        p.drawString(350, y-20, f"TOTAL: Rs. {total_revenue}")
        
        p.save()
        buffer.seek(0)
        return buffer
    except Exception as e:
        print(f"PDF Error: {e}")
        return None

def send_admin_notification(order_details, verification_code):
    if not bot: return
    try:
        items_list = db_manager.parse_order_items(order_details['items'])
        food_summary = "\n".join([f"• {item['name']} x {item['qty']}" for item in items_list])
        
        # Format: JAN28-1
        try:
             token_num = f"{datetime.now().strftime('%b%d').upper()}-{order_details.get('daily_token', '?')}"
        except: token_num = verification_code

        msg = (
            f"🚨 *NEW ORDER PAID!* ({token_num})\n"
            f"Amt: ₹{order_details['total_amount']}\n"
            f"User: {order_details.get('student_phone')}\n\n"
            f"{food_summary}"
        )
        
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Mark Delivered", callback_data=f"mark_delivered_{order_details['id']}"))
        
        for admin_id in ADMIN_CHAT_IDS:
            try: bot.send_message(admin_id, msg, reply_markup=kb, parse_mode='Markdown')
            except: pass
    except Exception as e:
        print(f"Notification error: {e}")

# --- HANDLERS MOVED UP ---
# Code is now organized with handlers before webhook


# --- NO MAIN LOOP ---
# Vercel handles the execution
