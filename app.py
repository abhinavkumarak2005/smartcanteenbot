# app.py

import os
import db_manager # Import the new db_manager
import telebot
from telebot.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, \
    ReplyKeyboardRemove
from dotenv import load_dotenv
from pathlib import Path
import qrcode
import uuid
import urllib.parse
import json
import threading
import time
from datetime import datetime, timedelta, timezone
import traceback
import logging
import razorpay
import re
from flask import Flask, request, jsonify, render_template_string, send_from_directory
import requests
import io # NEW: For in-memory file handling

# --- PROJECT CONFIGURATION & ROBUST .ENV LOADING ---
BASE_DIR = Path(__file__).resolve().parent
DOTENV_PATH = BASE_DIR / '.env'
# DELETED: STATIC_DIR is no longer needed

# Load environment variables using the explicit path
load_dotenv(dotenv_path=DOTENV_PATH)

# --- ENVIRONMENT CHECK AND RAZORPAY SETUP ---

# Required environment variables list
REQUIRED_ENV_VARS = [
    'BOT_TOKEN', 'RAZORPAY_KEY_ID', 'RAZORPAY_KEY_SECRET', 'BOT_PUBLIC_URL',
    'SUPABASE_URL', 'SUPABASE_SERVICE_KEY', 'SUPABASE_DB_URL', 'SUPABASE_QR_BUCKET_URL' # NEW: Supabase vars
]

# Check for required variables and exit if missing
for var in REQUIRED_ENV_VARS:
    if not os.getenv(var):
        print(f"❌ ERROR: Missing required environment variable: {var}")
        print("Please check your .env file and ensure all required keys are present.")
        exit(1)

# --- CONFIGURATION ---
TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_IDS = [int(num.strip()) for num in os.getenv('ADMIN_CHAT_IDS', '').split(',') if num.strip().isdigit()]
PAYEE_NAME = os.getenv('PAYEE_NAME', 'Canteen Staff')
# NEW: Get QR Code Base URL from Supabase Bucket
QR_CODE_BASE_URL = os.getenv('SUPABASE_QR_BUCKET_URL') 
WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', 'BOT_SECRET_9876').strip()
BOT_PUBLIC_URL = os.getenv('BOT_PUBLIC_URL')

# Razorpay Client Initialization
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')
RAZORPAY_CLIENT = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
print("✅ Razorpay Client initialized.")

# --- TELEGRAM SETUP ---
print(f"✅ Telegram Bot Token loaded successfully.")
try:
    logger = telebot.logger
    telebot.logger.setLevel(logging.INFO)
    bot = telebot.TeleBot(TOKEN)
except Exception as e:
    print(f"❌ Error initializing TeleBot: {e}")
    exit(1)

# --- FLASK WEB SERVER SETUP (For Webhooks) ---
# This 'app' object is what Vercel will run
app = Flask(__name__)

# --- NEW: IMPORT SUPABASE CLIENT FROM DB_MANAGER ---
# This client is used for file uploads
try:
    from db_manager import supabase
    if supabase is None:
        raise ImportError("Supabase client is None. Check environment variables.")
    print("✅ Supabase client imported successfully for file storage.")
except ImportError as e:
    print(f"❌ CRITICAL: Could not import Supabase client from db_manager. {e}")
    # This is fatal, the app can't upload QR codes.
    exit(1)


# --- NEW MENU STRUCTURE & TIME CONFIGURATION ---
# (This section is unchanged)
MENU_SECTIONS = {
    'breakfast': {'time': '9:00 - 11:30', 'start_hr': 9, 'end_hr': 11, 'end_min': 30},
    'lunch': {'time': '12:00 - 4:00', 'start_hr': 12, 'end_hr': 16, 'end_min': 0},
    'snacks': {'time': 'All Day', 'start_hr': 0, 'end_hr': 23, 'end_min': 59} # 24/7 equivalent
}
IST = timezone(timedelta(hours=5, minutes=30))
OPERATING_START_HOUR = 9
OPERATING_END_HOUR = 17 

# --- BOT AVAILABILITY CHECK ---
# (This section is unchanged)
def is_bot_available_now() -> bool:
    return True # Keep this as True if you want 24/7 operation

def unavailable_message(chat_id):
    bot.send_message(chat_id, "The canteen bot is currently unavailable. Please place your order only between 9:00 AM and 5:00 PM IST.")

# --- FLASK ENDPOINT REGISTRATION FUNCTION (All Web Routes) ---

def setup_flask_routes():
    """Registers all necessary Flask routes to prevent AssertionError."""

    @app.route('/', methods=['GET'])
    def root():
        return "Telegram Canteen Bot is running (Serverless).", 200
        
    # DELETED: The @app.route('/static/<path:filename>') is REMOVED.
    # Supabase Storage now serves all QR codes.

    @app.route('/order_success', methods=['GET'])
    def order_success():
        # (This function is unchanged)
        html_content = """
        <!DOCTYPE html>
        <html lang="en">
        <head>...[content omitted for brevity]...</head>
        <body>
            <div class="container">
                <h1>✅ Payment Confirmed!</h1>
                <p>Your payment was successfully received by Razorpay.</p>
                <p>Please switch back to the Telegram app now.<br>The bot will send your pickup QR code shortly!</p>
            </div>
        </body>
        </html>
        """
        return html_content

    @app.route('/razorpay/webhook', methods=['POST'])
    def razorpay_webhook():
        # (This function is unchanged)
        print("🚨 Webhook received from Razorpay.")
        payload_bytes = request.get_data()
        signature = request.headers.get('X-Razorpay-Signature')

        try:
            payload_str = payload_bytes.decode('utf-8')
            RAZORPAY_CLIENT.utility.verify_webhook_signature(payload_str, signature, WEBHOOK_SECRET)
            print("✅ Webhook signature verified successfully!")
        except Exception as e:
            print(f"❌ Webhook signature verification failed: {e}")
            return jsonify(success=False, message="Signature verification failed"), 400

        try:
            event = json.loads(payload_str)
            if event['event'] == 'payment.captured':
                payment_entity = event['payload']['payment']['entity']
                razorpay_order_id = payment_entity['order_id']
                rzp_order_details = RAZORPAY_CLIENT.order.fetch(razorpay_order_id)
                internal_order_id = rzp_order_details['notes']['internal_order_id']
                student_db_id = rzp_order_details['notes']['telegram_chat_id']
                print(f"✅ Payment captured for RZ ID: {razorpay_order_id}, Internal ID: {internal_order_id}")
                
                # Run in a thread for speed, even in serverless
                threading.Thread(
                    target=handle_successful_payment,
                    args=(int(internal_order_id), student_db_id)
                ).start()
        except Exception as e:
            print(f"❌ Error processing webhook payload: {e}")
            traceback.print_exc()
            return jsonify(success=False, message="Internal processing error"), 500

        return jsonify(success=True), 200

    @app.route('/order_display/<int:order_id>/<string:verification_code>', methods=['GET'])
    def order_display(order_id, verification_code):
        # (This function is unchanged)
        order_details = db_manager.get_order_details(order_id)
        if not order_details or order_details.get('pickup_code') != verification_code:
            return render_template_string("<h1>❌ Invalid Order</h1>"), 404
        
        # ... [Rest of the HTML template logic is unchanged, omitted for brevity] ...
        
        status = order_details.get('status', 'PENDING').upper()
        # ... (logic for status_color, status_message) ...
        items_list = db_manager.parse_order_items(order_details.get('items', '[]'))
        # ... (logic for items_html) ...
        student_chat_id = order_details['student_phone']
        display_contact = db_manager.get_user_phone(student_chat_id) or student_chat_id

        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>...[content omitted for brevity]...</head>
        <body>
            <div class="ticket">
                <h2>Canteen Order Ticket #{order_id}</h2>
                <div class="status-box" style="background-color: {status_color};">STATUS: {status_message}</div>
                <div class="pickup-code" style="color: {status_color};">
                    {verification_code}
                </div>
            </div>
        </body>
        </html>
        """
        return html_content, 200


# --- RAZORPAY API FUNCTIONS ---
def generate_razorpay_payment_link(internal_order_id, amount, student_phone):
    # (This function is unchanged)
    if not BOT_PUBLIC_URL or not BOT_PUBLIC_URL.startswith('http'):
        print("❌ CRITICAL ERROR: BOT_PUBLIC_URL is missing or invalid.")
        return None, None

    try:
        unique_reference_id = str(uuid.uuid4())
        notes = {
            "internal_order_id": str(internal_order_id),
            "telegram_chat_id": str(student_phone)
        }
        user_phone = db_manager.get_user_phone(student_phone)
        razorpay_contact = user_phone if user_phone else student_phone

        data = {
            "amount": int(amount * 100),  # Amount in paise
            "currency": "INR",
            "accept_partial": False,
            "reference_id": unique_reference_id,
            "description": f"Canteen Order #{internal_order_id} - {PAYEE_NAME}",
            "customer": {
                "contact": f"+{razorpay_contact}",
                "name": f"Telegram User {razorpay_contact}"
            },
            "notify": {"sms": False, "email": False},
            "callback_url": f"{BOT_PUBLIC_URL}/order_success",
            "callback_method": "get",
            "notes": notes
        }

        rzp_link = RAZORPAY_CLIENT.payment_link.create(data)

        if 'id' not in rzp_link:
            raise KeyError(f"Razorpay API response is invalid. Missing 'id'. Response keys: {rzp_link.keys()}")

        payment_url = rzp_link['short_url']
        razorpay_order_id = rzp_link['id']

        print(f"💰 Razorpay Payment Link created: {razorpay_order_id} (Ref ID: {unique_reference_id})")
        return razorpay_order_id, payment_url

    except Exception as e:
        print(f"❌ Error generating Razorpay payment link/order: {e}")
        traceback.print_exc()
        if isinstance(e, razorpay.errors.BadRequestError):
            raise razorpay.errors.BadRequestError(str(e))
        return None, None


# --- PAYMENT & QR CODE UTILITIES ---
def create_payment_keyboard(payment_link, order_id):
    # (This function is unchanged)
    try:
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.row(InlineKeyboardButton("💳 Pay Securely with Razorpay", url=payment_link))
        keyboard.row(InlineKeyboardButton("📋 Copy Payment Link", callback_data=f"copy_razorpay_{order_id}"))
        return keyboard
    except Exception as e:
        print(f"❌ Error creating payment keyboard: {e}")
        return None


def generate_payment_qr_code(payment_link, order_id):
    """
    MODIFIED: Generate QR code for payment link, upload to Supabase, and return public URL.
    """
    try:
        filename = f"razorpay_qr_{order_id}_{uuid.uuid4().hex[:8]}.png"
        
        # Generate QR in memory
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(payment_link)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="darkblue", back_color="white")

        # Save to an in-memory buffer
        img_buffer = io.BytesIO()
        qr_img.save(img_buffer, format='PNG')
        img_buffer.seek(0) # Rewind buffer to the beginning

        # Upload to Supabase Storage
        # We upload the raw bytes from the buffer
        supabase.storage.from_("qr-codes").upload(
            path=filename,
            file=img_buffer.getvalue(), # Use getvalue() for the full buffer
            file_options={"content-type": "image/png", "cache-control": "3600"}
        )
        
        # Get the public URL
        # We must join this manually as the client doesn't know the full public URL
        public_url = urllib.parse.urljoin(QR_CODE_BASE_URL + '/', filename)

        logging.info(f"Uploaded payment QR to Supabase: {public_url}")
        return public_url

    except Exception as e:
        if "No module named 'PIL'" in str(e):
            print("❌ CRITICAL ERROR: PIL/Pillow library is missing.")
        print(f"❌ Error generating/uploading payment QR code: {e}")
        traceback.print_exc()
        return None


def generate_pickup_qr_code(order_id, student_phone):
    """
    MODIFIED: Generates pickup QR, uploads to Supabase Storage, and returns public URL.
    """
    try:
        verification_code = f"{order_id}{datetime.now().strftime('%M%S')}"

        if not BOT_PUBLIC_URL:
            raise ValueError("BOT_PUBLIC_URL environment variable is not set.")

        path = f"order_display/{order_id}/{verification_code}"
        web_link = urllib.parse.urljoin(BOT_PUBLIC_URL.rstrip('/') + '/', path)
        filename = f"pickup_qr_{order_id}_{uuid.uuid4().hex[:8]}.png"

        # Generate QR in memory
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(web_link)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="darkgreen", back_color="white")

        # Save to an in-memory buffer
        img_buffer = io.BytesIO()
        qr_img.save(img_buffer, format='PNG')
        img_buffer.seek(0) # Rewind buffer

        # Upload to Supabase Storage
        supabase.storage.from_("qr-codes").upload(
            path=filename,
            file=img_buffer.getvalue(),
            file_options={"content-type": "image/png", "cache-control": "3600"}
        )
        
        # Get the public URL
        public_url = urllib.parse.urljoin(QR_CODE_BASE_URL + '/', filename)

        logging.info(f"Uploaded pickup QR to Supabase: {public_url}")
        return public_url, verification_code, web_link

    except Exception as e:
        if "No module named 'PIL'" in str(e):
            print("❌ CRITICAL ERROR: PIL/Pillow library is missing.")
        print(f"❌ Error generating/uploading pickup QR code: {e}")
        traceback.print_exc()
        return None, None, None


# --- KEYBOARD GENERATION FUNCTIONS ---
# (All functions from get_main_reply_keyboard() to get_confirmation_inline_keyboard()
# are UNCHANGED. They are omitted here for brevity.)
def get_main_reply_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=False)
    markup.add(KeyboardButton('Menu 🍽️'), KeyboardButton('Order Status 📊'))
    return markup
# ... [all other keyboard functions are identical] ...
def get_confirmation_inline_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_pay"),
               InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order"))
    return markup


# --- UTILITY FUNCTIONS ---
# (get_menu_text_with_sections and escape_markdown are UNCHANGED)
def get_menu_text_with_sections(is_admin: bool):
    # ... [function is identical] ...
    menu = db_manager.get_menu()
    items_by_section = {key: [] for key in MENU_SECTIONS.keys()}
    # ... [rest of logic] ...
    return menu_text

def escape_markdown(text):
    # ... [function is identical] ...
    if text is None: text = "N/A"
    else: text = str(text)
    escape_chars = r'_*`[]()~>#+-|=.!'
    return "".join(['\\' + char if char in escape_chars else char for char in text])


# (send_admin_message and send_admin_notification are UNCHANGED)
def send_admin_message(chat_id, text, parse_mode='MarkdownV2', reply_markup=None):
    # ... [function is identical] ...
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        # ... [fallback logic] ...
        pass

def send_admin_notification(order_details, verification_code):
    # ... [function is identical] ...
    try:
        # ... [logic to build notification_msg] ...
        for admin_id in ADMIN_CHAT_IDS:
            send_admin_message(admin_id, notification_msg, reply_markup=delivery_keyboard)
    except Exception as e:
        print(f"❌ Error in admin notification: {e}")
        traceback.print_exc()


def handle_successful_payment(internal_order_id, student_db_id):
    """Handles all post-payment logic (QR code generation, admin notification)."""
    
    # Needs to run in a thread, so we must make sure db_manager is available
    # In a serverless context, this function starts "fresh"
    # It's better to pass the db_manager module or re-import, but let's
    # assume global state holds for the thread's life.
    
    order_details = db_manager.get_order_details(internal_order_id)
    if not order_details:
        logging.error(f"Payment success for Order {internal_order_id}, but details not found in DB.")
        return

    db_manager.update_order_status(internal_order_id, 'paid')

    # --- CHANGED: This now returns a public URL, not a local file path ---
    ticket_qr_url, verification_code, web_link = generate_pickup_qr_code(
        internal_order_id, student_db_id
    )

    db_manager.update_order_pickup_code(internal_order_id, verification_code)
    service_type = order_details.get('service_type', 'N/A')

    if web_link is None:
        link_markdown = escape_markdown("None (Error during generation)")
    else:
        escaped_link_text = escape_markdown(web_link)
        link_markdown = f"[Click Here to View Ticket]({web_link})"

    pickup_msg = (
        f"🎉 Payment Confirmed\\! \\(Order ID\\: \\#{internal_order_id}\\)\n\n"
        f"Here is your Order QR Code for pickup\\!\n\n"
        f"Verification Code\\: *{escape_markdown(verification_code)}*\n"
        f"Service Type\\: {escape_markdown(service_type.title())}\n\n"
        f"For Pickup\\:\n"
        f"Scan the QR code below\\.\n"
        f"\\(Note\\: If you see a warning page, please click \\'Visit Site\\'\\.\\)\n\n"
        f"\\*Preparation Time\\*\\: Please visit the canteen counter in about 10\\-15 minutes\\.\n\n"
        f"Alternative Link\\: {link_markdown}"
    )

    db_manager.set_session_state(student_db_id, 'pickup_ready', internal_order_id)
    send_admin_notification(order_details, verification_code)

    main_keyboard = get_main_reply_keyboard()
    
    # --- CRITICAL CHANGE: Send photo from URL, not local file ---
    if ticket_qr_url:
        # We send the photo using the public URL from Supabase
        bot.send_photo(student_db_id, ticket_qr_url, caption=pickup_msg, parse_mode='MarkdownV2',
                       reply_markup=main_keyboard)
    else:
        # Fallback message
        fallback_msg = (
            f"🎉 \\*Payment Confirmed\\!\\* \n\n"
            f"❌ QR Code generation failed\\. Use the Verification Code and Alternative Link\\.\n\n"
            f"🆔 \\*Order ID\\*\\: \\#{internal_order_id}\n"
            # ... [rest of fallback message] ...
        )
        bot.send_message(student_db_id, fallback_msg, parse_mode='MarkdownV2', reply_markup=main_keyboard)
    
    # This line seems incorrect from your original code, it should be 'pickup_ready'
    # db_manager.set_session_state(student_db_id, 'waiting_for_payment', current_order_id)
    # Let's stick to the 'pickup_ready' set above.
    return


# (add_item_to_cart_and_prompt is UNCHANGED)
def add_item_to_cart_and_prompt(student_db_id, chat_id, message_id, item_id, quantity):
    # ... [function is identical] ...
    pass

# (view_archives_command_handler is UNCHANGED)
def view_archives_command_handler(chat_id):
    # ... [function is identical] ...
    pass

# (handle_admin_text_commands is UNCHANGED)
def handle_admin_text_commands(msg, chat_id):
    # ... [function is identical] ...
    pass

# (handle_admin_callbacks is UNCHANGED)
def handle_admin_callbacks(data, chat_id, message_id):
    # ... [function is identical] ...
    pass

# (prompt_for_phone_number is UNCHANGED)
def prompt_for_phone_number(student_db_id, chat_id):
    # ... [function is identical] ...
    pass

# (start_menu_flow is UNCHANGED)
def start_menu_flow(student_db_id, chat_id, message_id=None, error_msg=None):
    # ... [function is identical] ...
    pass

# (handle_status_check is UNCHANGED)
def handle_status_check(student_db_id, chat_id):
    # ... [function is identical] ...
    pass


# --- DELETED ---
# The functions `delete_old_qr_codes` and `start_cleanup_thread`
# have been REMOVED. This is handled by Supabase.


# --- TELEGRAM BOT HANDLERS ---
# (All @bot.message_handler and @bot.callback_query_handler
# functions are UNCHANGED. Omitted for brevity.)
@bot.message_handler(commands=['start'])
def send_welcome(message):
    # ... [function is identical] ...
    pass

@bot.message_handler(func=lambda message: message.text in ['Menu 🍽️', 'Order Status 📊', 'Admin Panel ⚙️', 'Orders 📦'])
def handle_reply_keyboard_buttons(message):
    # ... [function is identical] ...
    pass

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    # ... [function is identical] ...
    pass

@bot.message_handler(content_types=['text'])
def handle_text_messages(message):
    # ... [function is identical] ...
    pass


# --- TELEGRAM BOT WEBHOOK SETUP (CRITICALLY MODIFIED) ---

# DELETED: `setup_bot_environment()` is GONE.
# DELETED: `set_webhook_and_run_flask()` is GONE.
# DELETED: `run_polling_service()` is GONE.
# DELETED: `if __name__ == '__main__':` is GONE.

# --- NEW: VERCEL ENTRY POINT ---

# 1. Register all Flask routes
# This is called AT THE TOP LEVEL so Vercel registers the routes.
setup_flask_routes()


# 2. Add the Telegram webhook route for Vercel
# This is the *only* entry point Vercel needs for Telegram.
@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    """Telegram Webhook endpoint to receive updates."""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        
        # Process the update in a separate thread
        # This is crucial for serverless to avoid timeouts
        threading.Thread(target=bot.process_new_updates, args=[[update]]).start()
        
        return 'OK', 200
    else:
        return 'Bad Request', 403

# Vercel will automatically find and run the 'app' object.
# No `app.run()` is needed.
