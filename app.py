# app.py

import os
# Ensure db_manager is imported AFTER dotenv load if it uses env vars at import time
from dotenv import load_dotenv
from pathlib import Path

# --- PROJECT CONFIGURATION & ROBUST .ENV LOADING ---
BASE_DIR = Path(__file__).resolve().parent
DOTENV_PATH = BASE_DIR / '.env'

# Load environment variables using the explicit path BEFORE other imports
# that might depend on them (like db_manager)
load_dotenv(dotenv_path=DOTENV_PATH)

# --- Now import modules that rely on environment variables ---
import db_manager # Import the new db_manager
import telebot
from telebot.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, \
    ReplyKeyboardRemove
import qrcode
import uuid
import urllib.parse
import json
import threading # Still needed for Razorpay webhook processing
import time
from datetime import datetime, timedelta, timezone
import traceback
import logging
import razorpay
import re
from flask import Flask, request, jsonify, render_template_string
import requests
import io # For in-memory file handling

# --- ENVIRONMENT CHECK ---
# Configure logging AFTER imports but before using logging
# Ensure logging is configured before any logging calls are made
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Check essential variables AFTER loading dotenv
REQUIRED_ENV_VARS = [
    'BOT_TOKEN', 'RAZORPAY_KEY_ID', 'RAZORPAY_KEY_SECRET', 'BOT_PUBLIC_URL',
    'SUPABASE_URL', 'SUPABASE_SERVICE_KEY', 'SUPABASE_DB_URL', 'SUPABASE_QR_BUCKET_URL'
]
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
if missing_vars:
    # Log critical error - Vercel will show this in deployment logs
    logging.critical(f"❌ FATAL ERROR: Missing required environment variables: {', '.join(missing_vars)}")
    # Exit if critical variables are missing, preventing the app from starting incorrectly
    exit(1)
else:
    logging.info("✅ All required environment variables are present.")


# --- CONFIGURATION ---
TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_IDS = [int(num.strip()) for num in os.getenv('ADMIN_CHAT_IDS', '').split(',') if num.strip().isdigit()]
PAYEE_NAME = os.getenv('PAYEE_NAME', 'Canteen Staff')
# Use the public URL of your Supabase bucket
QR_CODE_BASE_URL = os.getenv('SUPABASE_QR_BUCKET_URL').rstrip('/') # Ensure no trailing slash
WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', '').strip() # Default to empty if not set
BOT_PUBLIC_URL = os.getenv('BOT_PUBLIC_URL').rstrip('/')

# Razorpay Client Initialization
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')
try:
    RAZORPAY_CLIENT = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    logging.info("✅ Razorpay Client initialized.")
except Exception as e:
    logging.error(f"❌ Error initializing Razorpay Client: {e}")
    RAZORPAY_CLIENT = None # Ensure it's None if init fails


# --- TELEGRAM SETUP ---
logging.info(f"✅ Telegram Bot Token loaded successfully.")
try:
    # Set log level for telebot
    telebot_logger = telebot.logger
    # You might want DEBUG during testing, INFO in production
    telebot_logger.setLevel(logging.INFO)

    bot = telebot.TeleBot(TOKEN)
    # Test connection (optional but recommended)
    bot_info = bot.get_me()
    logging.info(f"✅ Telegram Bot initialized: {bot_info.username} (ID: {bot_info.id})")

except Exception as e:
    logging.critical(f"❌ FATAL ERROR initializing TeleBot: {e}")
    # If the bot can't initialize, the app is useless
    exit(1)

# --- FLASK WEB SERVER SETUP (For Vercel entry point) ---
app = Flask(__name__)

# --- IMPORT SUPABASE CLIENT FROM DB_MANAGER ---
# This client is used specifically for file uploads
try:
    # Ensure db_manager.supabase is not None after db_manager potentially failing initialization
    if db_manager.supabase is None:
        raise ImportError("Supabase client in db_manager is None. Check db_manager logs and env vars.")
    supabase = db_manager.supabase # Assign to local variable
    logging.info("✅ Supabase client imported successfully for file storage.")
except AttributeError:
     logging.critical("❌ FATAL ERROR: 'supabase' client not found in db_manager. Check db_manager.py initialization.")
     exit(1)
except ImportError as e:
    logging.critical(f"❌ FATAL ERROR: Could not import Supabase client from db_manager. {e}")
    exit(1)


# --- MENU STRUCTURE & TIME CONFIGURATION ---
MENU_SECTIONS = {
    'breakfast': {'time': '9:00 - 11:30', 'start_hr': 9, 'end_hr': 11, 'end_min': 30},
    'lunch': {'time': '12:00 - 4:00', 'start_hr': 12, 'end_hr': 16, 'end_min': 0},
    'snacks': {'time': 'All Day', 'start_hr': 0, 'end_hr': 23, 'end_min': 59}
}
IST = timezone(timedelta(hours=5, minutes=30))
OPERATING_START_HOUR = 9
OPERATING_END_HOUR = 17

# --- BOT AVAILABILITY CHECK ---
def is_bot_available_now() -> bool:
    """Checks if the current time is within operating hours in IST."""
    # --- Uncomment the block below to enable time restrictions ---
    # now_ist = datetime.now(IST)
    # current_hour = now_ist.hour
    # # Available from START_HOUR (inclusive) up to END_HOUR (exclusive)
    # is_within_hours = OPERATING_START_HOUR <= current_hour < OPERATING_END_HOUR
    # if not is_within_hours:
    #      logging.warning(f"Bot access denied outside operating hours ({current_hour} IST).")
    # return is_within_hours
    # --- Remove or comment out the line below to enable time restrictions ---
    return True # Currently set to always available

def unavailable_message(chat_id):
    """Sends the standard unavailability message using the enhanced sender."""
    # Use the enhanced send function which includes logging
    send_telegram_message(chat_id, "The canteen bot is currently unavailable. Please place your order only between 9:00 AM and 5:00 PM IST.")

# --- FLASK ROUTES ---

@app.route('/', methods=['GET'])
def root():
    """Basic health check endpoint."""
    logging.info("Root endpoint / accessed.")
    return "Telegram Canteen Bot is running (Serverless).", 200

# /static/ route is removed - Supabase serves QR codes

@app.route('/order_success', methods=['GET'])
def order_success():
    """Page shown in browser after successful Razorpay payment."""
    logging.info("Order success page /order_success accessed.")
    # Simple HTML, consider using templates for more complex pages
    html_content = """
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Payment Successful</title><style>body{font-family: sans-serif; text-align: center; background-color: #f0f4f8; color: #333; padding: 50px;} .container{background-color: #fff; border-radius: 8px; padding: 30px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 400px; margin: 40px auto;} h1{color: #4CAF50;} p{margin-bottom: 20px;}</style></head>
    <body><div class="container"><h1>✅ Payment Confirmed!</h1><p>Your payment was successfully processed.</p><p>Please return to Telegram. Your order QR code will be sent shortly!</p></div></body></html>
    """
    return html_content

@app.route('/razorpay/webhook', methods=['POST'])
def razorpay_webhook():
    """Endpoint for Razorpay payment notifications."""
    logging.info("Razorpay webhook received.")
    payload_bytes = request.get_data()
    signature = request.headers.get('X-Razorpay-Signature')

    if not WEBHOOK_SECRET:
         logging.error("❌ Razorpay Webhook Secret is not configured in environment variables.")
         return jsonify(success=False, message="Webhook secret not configured"), 500

    if not RAZORPAY_CLIENT:
         logging.error("❌ Razorpay Client is not initialized. Cannot verify webhook.")
         return jsonify(success=False, message="Razorpay client not available"), 500

    try:
        payload_str = payload_bytes.decode('utf-8')
        RAZORPAY_CLIENT.utility.verify_webhook_signature(payload_str, signature, WEBHOOK_SECRET)
        logging.info("✅ Razorpay webhook signature verified.")
    except razorpay.errors.SignatureVerificationError:
        logging.warning("⚠️ Razorpay webhook signature verification failed.")
        return jsonify(success=False, message="Signature verification failed"), 400
    except Exception as e:
        logging.error(f"❌ Error verifying Razorpay webhook signature: {e}")
        return jsonify(success=False, message="Internal error during signature verification"), 500

    try:
        event = json.loads(payload_str)
        logging.info(f"Processing Razorpay event: {event.get('event')}")

        # Check if it's the 'payment.captured' event
        if event.get('event') == 'payment.captured':
            payment_entity = event.get('payload', {}).get('payment', {}).get('entity', {})
            razorpay_order_id = payment_entity.get('order_id')
            notes = payment_entity.get('notes', {})
            internal_order_id = notes.get('internal_order_id')
            student_db_id = notes.get('telegram_chat_id')

            if not razorpay_order_id or not internal_order_id or not student_db_id:
                 logging.warning(f"⚠️ Missing data in webhook payload: RzpID={razorpay_order_id}, InternalID={internal_order_id}, ChatID={student_db_id}")
                 # Still return 200 OK to Razorpay, but log the issue
                 return jsonify(success=True, message="Payload missing required notes"), 200

            logging.info(f"✅ Payment captured for RZ Order ID: {razorpay_order_id}, Internal Order ID: {internal_order_id}")

            # Process payment in a background thread to respond quickly
            # Ensure args are correctly typed for the target function
            threading.Thread(
                target=handle_successful_payment,
                args=(int(internal_order_id), str(student_db_id)) # Cast to expected types
            ).start()
        else:
             logging.info(f"Ignoring non-captured Razorpay event: {event.get('event')}")

    except json.JSONDecodeError as e:
        logging.error(f"❌ Error decoding Razorpay webhook JSON payload: {e}")
        return jsonify(success=False, message="Invalid JSON payload"), 400
    except Exception as e:
        logging.error(f"❌ Error processing Razorpay webhook payload: {e}")
        traceback.print_exc()
        return jsonify(success=False, message="Internal processing error"), 500

    # Acknowledge receipt to Razorpay immediately
    return jsonify(success=True), 200


@app.route('/order_display/<int:order_id>/<string:verification_code>', methods=['GET'])
def order_display(order_id, verification_code):
    """Displays the digital order ticket page accessed by QR code scan."""
    logging.info(f"Order display page requested for Order ID: {order_id}")
    order_details = db_manager.get_order_details(order_id)

    if not order_details or order_details.get('pickup_code') != verification_code:
        logging.warning(f"Invalid access attempt for order display: Order={order_id}, Code={verification_code}")
        # Basic error page, consider a more user-friendly template
        return render_template_string("<h1>❌ Invalid Order or Code</h1><p>Please scan the correct QR code.</p>"), 404

    status = order_details.get('status', 'PENDING').upper()
    status_map = {
        'PAID': ('#4CAF50', 'READY FOR PICKUP'),
        'PICKUP_READY': ('#4CAF50', 'READY FOR PICKUP'), # Treat same as PAID visually
        'DELIVERED': ('#2196F3', 'COLLECTED'),
        'CANCELLED': ('#F44336', 'CANCELLED'),
        'EXPIRED': ('#F44336', 'EXPIRED'), # Treat same as CANCELLED visually
        'PENDING': ('#FF9800', 'PENDING'),
        'PAYMENT_PENDING': ('#FF9800', 'PAYMENT PENDING') # Treat same as PENDING visually
    }
    status_color, status_message = status_map.get(status, ('#757575', status)) # Default grey

    items_list = db_manager.parse_order_items(order_details.get('items', '[]'))
    items_html = "".join(
        f"<li>{item.get('name', 'N/A').title()} x {item.get('qty', 1)} (₹{item.get('price', 0.0):.2f})</li>"
        for item in items_list
    ) if items_list else "<li>No items found</li>"

    # Use chat ID as fallback if phone number wasn't collected/found
    student_chat_id = order_details.get('student_phone', 'N/A')
    display_contact = db_manager.get_user_phone(student_chat_id) or student_chat_id

    # Using f-string for HTML templating (consider Jinja2 for more complex pages)
    html_content = f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Order #{order_id} Ticket</title><style>body{{font-family: sans-serif; background-color: #f4f7f6; margin: 0; padding: 20px; display: flex; justify-content: center; align-items: center; min-height: 100vh;}} .ticket{{background-color: #fff; border: 1px solid #ddd; border-radius: 10px; padding: 25px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); width: 90%; max-width: 450px; text-align: center;}} h2{{color: #333; margin-top: 0; border-bottom: 1px dashed #ccc; padding-bottom: 10px;}} .status-box{{background-color: {status_color}; color: white; padding: 12px; border-radius: 5px; font-weight: bold; margin: 20px 0; font-size: 1.1em; text-transform: uppercase;}} .detail-grid{{display: grid; grid-template-columns: auto 1fr; gap: 8px 15px; margin-bottom: 20px; text-align: left;}} .detail-label{{font-weight: bold; color: #555;}} .detail-value{{color: #333;}} .items ul{{list-style: none; padding: 0; margin: 10px 0 0 0; text-align: left;}} .items li{{background-color: #f9f9f9; padding: 8px 12px; border-radius: 4px; margin-bottom: 6px; border: 1px solid #eee;}} .pickup-code{{font-size: 2.8em; color: {status_color}; font-weight: bold; margin-top: 15px; letter-spacing: 2px;}}</style></head>
    <body><div class="ticket"><h2>Canteen Order #{order_id}</h2><div class="status-box">{status_message}</div><div class="detail-grid"><div class="detail-label">Total:</div><div class="detail-value">₹{order_details.get('total_amount', 0.0):.2f}</div><div class="detail-label">Service:</div><div class="detail-value">{order_details.get('service_type', 'N/A').replace('_', ' ').title()}</div><div class="detail-label">Contact:</div><div class="detail-value">{display_contact}</div></div><div class="items"><strong>Items Ordered:</strong><ul>{items_html}</ul></div><p style="margin-top: 25px; color: #555;">Present this code at the counter:</p><div class="pickup-code">{verification_code}</div></div></body></html>
    """
    return html_content, 200

# --- RAZORPAY API FUNCTIONS ---
def generate_razorpay_payment_link(internal_order_id, amount, student_phone):
    """Creates a Razorpay Payment Link."""
    if not BOT_PUBLIC_URL or not BOT_PUBLIC_URL.startswith('http'):
        logging.error("❌ CRITICAL: BOT_PUBLIC_URL is missing or invalid for Razorpay callback.")
        return None, None
    if not RAZORPAY_CLIENT:
        logging.error("❌ Razorpay Client not initialized, cannot create payment link.")
        return None, None

    try:
        # Use a UUID for reference_id to ensure uniqueness
        unique_reference_id = str(uuid.uuid4())
        notes = {
            "internal_order_id": str(internal_order_id),
            "telegram_chat_id": str(student_phone) # Pass chat_id for webhook lookup
        }
        # Get stored phone number, fallback to chat_id if not available
        user_phone = db_manager.get_user_phone(student_phone)
        # Razorpay requires a contact, format might need country code
        razorpay_contact = user_phone if user_phone else str(student_phone)
        # Basic formatting attempt for Indian numbers
        if re.match(r"^\d{10}$", razorpay_contact):
             razorpay_contact = f"+91{razorpay_contact}"
        elif re.match(r"^\+\d+$", razorpay_contact):
             pass # Already has country code
        else:
             # Fallback if it's neither 10 digits nor starts with +
             logging.warning(f"Uncertain contact format for Razorpay: {razorpay_contact}. Assuming needs +91.")
             razorpay_contact = f"+91{razorpay_contact.lstrip('+')}" # Add +91, remove existing + if any

        data = {
            "amount": int(amount * 100),  # Amount in paise
            "currency": "INR",
            "accept_partial": False,
            "reference_id": unique_reference_id,
            "description": f"Canteen Order #{internal_order_id} - {PAYEE_NAME}",
            "customer": {
                "contact": razorpay_contact,
                "name": f"User {student_phone}" # Use chat_id as fallback name
            },
            "notify": {"sms": False, "email": False}, # Disable Razorpay notifications
            "callback_url": f"{BOT_PUBLIC_URL}/order_success", # User redirect URL
            "callback_method": "get",
            "notes": notes # Crucial for linking webhook back to order
        }

        rzp_link_response = RAZORPAY_CLIENT.payment_link.create(data)

        # Validate response
        payment_url = rzp_link_response.get('short_url')
        razorpay_link_id = rzp_link_response.get('id') # This is the payment link ID

        if not payment_url or not razorpay_link_id:
            logging.error(f"❌ Razorpay API response invalid. Missing URL or ID. Response: {rzp_link_response}")
            return None, None

        logging.info(f"💰 Razorpay Payment Link created: {razorpay_link_id} for Order {internal_order_id}")
        return razorpay_link_id, payment_url

    except razorpay.errors.BadRequestError as e:
         logging.error(f"❌ Razorpay API Bad Request Error: {e}")
         # Raise it so the calling function can handle UI feedback
         raise e
    except requests.exceptions.ConnectionError as e:
         logging.error(f"❌ Network connection error during Razorpay link generation: {e}")
         # Raise it for UI feedback
         raise e
    except Exception as e:
        logging.error(f"❌ Unexpected error generating Razorpay payment link: {e}")
        traceback.print_exc()
        return None, None

# --- PAYMENT & QR CODE UTILITIES ---
def create_payment_keyboard(payment_link, order_id):
    """Creates inline keyboard with Razorpay payment button and copy link."""
    markup = InlineKeyboardMarkup(row_width=1)
    try:
        markup.row(InlineKeyboardButton("💳 Pay Securely Online", url=payment_link))
        # Copy link callback requires handling in handle_callbacks
        markup.row(InlineKeyboardButton("📋 Copy Payment Link", callback_data=f"copy_razorpay_{order_id}"))
        return markup
    except Exception as e:
        logging.error(f"❌ Error creating payment keyboard for order {order_id}: {e}")
        # Return a simple keyboard as fallback? Or None?
        return None

def _generate_and_upload_qr(data_to_encode, base_filename, fill_color):
    """Internal helper to generate QR, upload to Supabase, return public URL."""
    if not supabase:
        logging.error("❌ Supabase client is not available for QR upload.")
        return None

    try:
        filename = f"{base_filename}_{uuid.uuid4().hex[:8]}.png"

        # Generate QR code in memory
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(data_to_encode)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fill_color, back_color="white")

        # Save to an in-memory bytes buffer
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0) # Reset buffer position to the beginning for reading

        # Upload the bytes buffer to Supabase Storage
        upload_path = filename # Store at the root of the bucket
        response = supabase.storage.from_("qr-codes").upload(
            path=upload_path,
            file=buffer, # Pass the buffer directly
            file_options={"content-type": "image/png", "cache-control": "3600", "upsert": "false"} # Cache for 1 hr, don't overwrite
        )

        # Construct the public URL manually (more reliable than get_public_url sometimes)
        # Ensure QR_CODE_BASE_URL ends with a '/' for correct joining
        base_url = QR_CODE_BASE_URL if QR_CODE_BASE_URL.endswith('/') else QR_CODE_BASE_URL + '/'
        public_url = urllib.parse.urljoin(base_url, upload_path)

        logging.info(f"✅ QR code '{filename}' uploaded. URL: {public_url}")
        return public_url

    except Exception as e:
        # Check for specific library missing error
        if "No module named 'PIL'" in str(e) or "No module named 'qrcode'" in str(e):
            logging.critical(f"❌ CRITICAL: Missing required library for QR generation (Pillow or qrcode): {e}")
        else:
            logging.error(f"❌ Error generating/uploading QR code '{base_filename}': {e}")
            traceback.print_exc()
        return None

def generate_payment_qr_code(payment_link, order_id):
    """Generate QR for payment link, upload to Supabase, return URL."""
    return _generate_and_upload_qr(payment_link, f"pay_qr_{order_id}", "darkblue")

def generate_pickup_qr_code(order_id, student_phone):
    """Generate QR for pickup/order display, upload to Supabase, return URL, code, web_link."""
    try:
        # Generate a simple verification code (consider making it more robust)
        verification_code = f"{order_id}{datetime.now().strftime('%M%S')}"

        if not BOT_PUBLIC_URL:
            raise ValueError("BOT_PUBLIC_URL environment variable is not set for pickup QR.")

        # Construct the URL the QR code will point to
        display_path = f"order_display/{order_id}/{verification_code}"
        # Ensure correct joining of base URL and path
        web_link = urllib.parse.urljoin(BOT_PUBLIC_URL + '/', display_path)

        # Generate and upload the QR code containing the web_link
        public_url = _generate_and_upload_qr(web_link, f"pickup_qr_{order_id}", "darkgreen")

        if public_url:
            return public_url, verification_code, web_link
        else:
            # Error occurred during QR generation/upload
            return None, verification_code, web_link # Still return code/link if QR fails

    except Exception as e:
        logging.error(f"❌ Error in generate_pickup_qr_code logic for order {order_id}: {e}")
        traceback.print_exc()
        # Return Nones, but maybe code generation succeeded? Difficult state.
        return None, None, None


# --- KEYBOARD GENERATION FUNCTIONS ---
# (These functions build the Telegram keyboard layouts - mostly unchanged)

def get_main_reply_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=False)
    markup.add(KeyboardButton('Menu 🍽️'), KeyboardButton('Order Status 📊'))
    return markup

def get_admin_reply_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=False)
    markup.add(KeyboardButton('Admin Panel ⚙️'), KeyboardButton('Orders 📦'))
    return markup

def get_orders_dashboard_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("📦 Today's Orders", callback_data="admin_orders_today"),
        # Archive feature removed as cleanup is now automated by pg_cron
        # InlineKeyboardButton("🗄️ Archived Orders", callback_data="admin_orders_archive")
    )
    markup.row(InlineKeyboardButton("↩️ Back to Main Panel", callback_data="admin_dashboard"))
    return markup

def get_admin_dashboard_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("📋 View/Edit Menu", callback_data="admin_menu"),
        InlineKeyboardButton("📈 View Stats", callback_data="admin_stats")
    )
    markup.row(InlineKeyboardButton("❓ Instructions", callback_data="admin_help"))
    return markup

def get_menu_inline_keyboard(user_id):
    """Generates the Inline Keyboard for item selection. Shows ID for admins."""
    menu = db_manager.get_menu()
    markup = InlineKeyboardMarkup(row_width=2) # Prefer 2 columns for menu items
    buttons = []

    if not menu:
         # Add a message or disable if menu is empty? For now, just empty keyboard.
         pass
    else:
        # Check if the current user is an admin
        # Convert user_id to int for comparison if ADMIN_CHAT_IDS are ints
        try:
            is_admin = int(user_id) in ADMIN_CHAT_IDS
        except ValueError:
            is_admin = False # Treat as non-admin if user_id is not convertible to int

        for item in menu:
            item_id = item['id']
            name = item.get('name', 'N/A').title()
            price = item.get('price', 0.0)
            # Format button text: Include ID only for admins
            button_text = f"{name} (₹{price:.2f})"
            if is_admin:
                button_text = f"ID {item_id}: {button_text}"

            buttons.append(InlineKeyboardButton(button_text, callback_data=f"item:{item_id}"))

    # Arrange buttons in rows of 2
    for i in range(0, len(buttons), 2):
        markup.row(*buttons[i:i+2]) # Unpack the pair of buttons into the row

    # Always add Cancel button at the end
    markup.row(InlineKeyboardButton("Cancel Order ❌", callback_data="cancel_order"))
    return markup

def get_phone_entry_keyboard():
    """Reply keyboard prompting user to type number (removed after one use)."""
    markup = ReplyKeyboardMarkup(row_width=1, resize_keyboard=True, one_time_keyboard=True)
    # No "Share Contact" button, rely on typed input for simplicity
    markup.add(KeyboardButton('Cancel Order ❌')) # Provide an exit option
    return markup

def get_quantity_inline_keyboard(item_id):
    """Inline keyboard for selecting quantity (1-5) or typing a custom amount."""
    markup = InlineKeyboardMarkup(row_width=5)
    qty_buttons = [InlineKeyboardButton(str(i), callback_data=f"qty:{item_id}:{i}") for i in range(1, 6)]
    markup.row(*qty_buttons)
    markup.row(InlineKeyboardButton("✍️ Type Quantity (>5)", callback_data=f"type_qty:{item_id}"))
    markup.row(InlineKeyboardButton("↩️ Back to Menu", callback_data="menu_start"))
    return markup

def get_add_more_inline_keyboard():
    """Inline keyboard asking to add more items or checkout."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add More Items", callback_data="add_more"),
        InlineKeyboardButton("🛒 Proceed to Checkout", callback_data="checkout")
    )
    return markup

def get_service_type_inline_keyboard():
    """Inline keyboard for choosing Dine In or Parcel."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🍴 Dine In", callback_data="service:dine_in"),
        InlineKeyboardButton("📦 Parcel/Takeaway", callback_data="service:parcel")
    )
    return markup

def get_confirmation_inline_keyboard():
    """Inline keyboard for final order confirmation before payment."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_pay"),
        InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order")
    )
    return markup

# --- UTILITY FUNCTIONS ---

def get_menu_text_with_sections(is_admin: bool):
    """Formats the menu text with sections, hiding IDs for non-admins."""
    menu = db_manager.get_menu()
    if not menu:
        return "😔 Sorry, the menu is currently empty."

    items_by_section = {key: [] for key in MENU_SECTIONS.keys()}
    # Ensure all items fall into a section, default to 'snacks'
    for item in menu:
        section = item.get('section', 'snacks').lower()
        items_by_section.setdefault(section, []).append(item)

    menu_text = "🍽️ **Digital Canteen Menu** 📋\n\n"
    found_items = False
    for section, data in MENU_SECTIONS.items():
        time_str = data.get('time', 'N/A')
        section_name = section.title()
        section_items = items_by_section.get(section, [])

        if section_items:
             found_items = True
             menu_text += f"*{section_name}* ({time_str})\n"
             for item in section_items:
                 # Ensure price is formatted correctly, handle potential None
                 price = item.get('price')
                 price_str = f"{price:.2f}" if price is not None else "N/A"
                 item_display = f"{item.get('name', 'N/A').title()} - *₹{price_str}*"
                 if is_admin:
                     menu_text += f"  - ID {item.get('id', '?')}: {item_display}\n"
                 else:
                     menu_text += f"  - {item_display}\n"
             menu_text += "\n"
        # Optionally show empty sections:
        # else:
        #     menu_text += f"*{section_name}* ({time_str})\n  - *No items available.*\n\n"

    if not found_items:
         return "😔 Sorry, the menu seems empty right now."

    # Add prompt based on user type
    if not is_admin:
        menu_text += "*Select an item below to begin.*"
    # For admin, the prompt is handled in the callback/command handler

    return menu_text

def escape_markdown(text):
    """Escapes characters for Telegram MarkdownV2."""
    if text is None: text = "" # Return empty string for None
    text = str(text)
    # Escape reserved characters for MarkdownV2
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    # Use re.sub for efficient replacement
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# --- ENHANCED LOGGING for send_telegram_message ---
def send_telegram_message(chat_id, text, **kwargs):
    """Wrapper for bot.send_message with more detailed error handling."""
    logging.info(f"Attempting to send message to {chat_id}: '{text[:50]}...'") # Log start
    try:
        response = bot.send_message(chat_id, text, **kwargs)
        logging.info(f"Successfully sent message to {chat_id}. Message ID: {response.message_id}") # Log success
        return True # Indicate success
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"❌ Telegram API Error sending to {chat_id}: {e.error_code} - {e.description}")
        # Log specifics if available
        if "bot was blocked by the user" in str(e):
             logging.warning(f"Bot blocked by user {chat_id}. Cannot send message.")
        elif "chat not found" in str(e):
             logging.warning(f"Chat {chat_id} not found.")
        # Add more specific checks if needed
        return False # Indicate failure
    except Exception as e:
        logging.error(f"❌ Unexpected error in send_telegram_message to {chat_id}: {e}")
        traceback.print_exc() # Print full traceback for unexpected errors
        return False # Indicate failure

def send_admin_message(admin_id, text, parse_mode='MarkdownV2', reply_markup=None):
    """Sends a message to a specific admin ID with fallback parsing."""
    if not send_telegram_message(admin_id, text, parse_mode=parse_mode, reply_markup=reply_markup):
        logging.warning(f"Failed to send admin message ({parse_mode}) to {admin_id}. Falling back.")
        if not send_telegram_message(admin_id, text, parse_mode='Markdown', reply_markup=reply_markup):
            logging.error(f"Failed to send admin message (Markdown) to {admin_id}. Sending plain text.")
            send_telegram_message(admin_id, text, parse_mode=None, reply_markup=reply_markup)


def send_admin_notification(order_details, verification_code):
    """Sends a formatted notification about a new paid order to all admins."""
    if not order_details:
        logging.warning("send_admin_notification called with no order_details.")
        return

    order_id = order_details.get('id', 'N/A')
    try:
        items_list = db_manager.parse_order_items(order_details.get('items', '[]'))
        service_type = order_details.get('service_type', 'N/A').replace('_', ' ').title()
        student_chat_id = order_details.get('student_phone', 'N/A')
        collected_phone = db_manager.get_user_phone(student_chat_id)
        display_identifier = collected_phone or student_chat_id

        # Escape dynamic content for MarkdownV2
        items_summary = "\n".join([
            # Ensure price is handled correctly if None
            f"• {escape_markdown(item.get('name', 'Item').title())} x {item.get('qty', 1)} \\(₹{item.get('price', 0.0):.2f}\\)"
            for item in items_list
        ]) if items_list else escape_markdown("No items listed")

        total_amount = order_details.get('total_amount')
        total_amount_esc = escape_markdown(f"{total_amount:.2f}") if total_amount is not None else "N/A"
        order_id_esc = escape_markdown(str(order_id))
        verification_code_esc = escape_markdown(verification_code or 'N/A')
        identifier_esc = escape_markdown(display_identifier)
        service_type_esc = escape_markdown(service_type)

        # Keyboard for admin to mark as delivered
        delivery_keyboard = InlineKeyboardMarkup(row_width=1).add(
            InlineKeyboardButton("✅ Mark as Delivered", callback_data=f"delivered:{order_id}")
        )

        notification_msg = (
            f"🚨 *NEW ORDER PAID* 🚨\n\n"
            f"🆔 *Order ID:* \\#{order_id_esc}\n"
            f"🔢 *Verification Code:* `{verification_code_esc}`\n"
            f"👤 *Customer:* `{identifier_esc}`\n"
            f"💰 *Total:* ₹{total_amount_esc}\n"
            f"🍽️ *Service:* {service_type_esc}\n\n"
            f"📝 *Items Ordered:*\n{items_summary}\n\n"
            f"🟢 *Status:* Ready for Preparation\n"
            f"*Action:* Prepare order & tap below when collected\\."
        )

        # Send to all configured admin chat IDs
        for admin_id in ADMIN_CHAT_IDS:
            send_admin_message(admin_id, notification_msg, reply_markup=delivery_keyboard)
        logging.info(f"Admin notification sent for Order ID: {order_id}")

    except Exception as e:
        logging.error(f"❌ Error sending admin notification for Order ID {order_id}: {e}")
        traceback.print_exc()

def handle_successful_payment(internal_order_id, student_db_id):
    """
    Handles post-payment logic: update status, generate QR, notify user & admin.
    Runs in a background thread from the webhook handler.
    """
    logging.info(f"Processing successful payment for Order ID: {internal_order_id}, User: {student_db_id}")
    order_details = db_manager.get_order_details(internal_order_id)
    if not order_details:
        logging.error(f"Payment success handler: Order {internal_order_id} not found in DB.")
        # Optionally, try to notify the user something went wrong
        send_telegram_message(student_db_id, "✅ Payment received, but there was an error retrieving your order details. Please contact staff.")
        return

    # Update order status to 'paid'
    if not db_manager.update_order_status(internal_order_id, 'paid'):
         logging.error(f"Failed to update order {internal_order_id} status to 'paid'.")
         # Proceed anyway, but log the error

    # Generate pickup QR code (URL), verification code, and web link
    ticket_qr_url, verification_code, web_link = generate_pickup_qr_code(
        internal_order_id, student_db_id
    )

    # Store the verification code in the database
    if verification_code:
        if not db_manager.update_order_pickup_code(internal_order_id, verification_code):
             logging.error(f"Failed to save pickup code for order {internal_order_id}.")
             # Verification code might be lost, but proceed with notification

    service_type = order_details.get('service_type', 'N/A').replace('_', ' ').title()

    # Prepare user message components
    link_markdown = escape_markdown("(Ticket link generation failed)") # Default if web_link is None
    if web_link:
         link_markdown = f"[{escape_markdown('Click Here to View Ticket')}]({web_link})" # Link target must NOT be escaped

    pickup_msg = (
        f"🎉 *Payment Confirmed\\!* \\(Order \\#{internal_order_id}\\)\n\n"
        f"Your order is being prepared\\. Please show this QR code at the counter for pickup\\.\n\n"
        f"*Verification Code:* `{escape_markdown(verification_code or 'N/A')}`\n"
        f"*Service:* {escape_markdown(service_type)}\n\n"
        f"*Est\\. Prep Time:* ~10\\-15 minutes\\.\n"
        f"*Pickup:* Scan QR below or {link_markdown}\\."
    )

    # Update user state
    db_manager.set_session_state(student_db_id, 'pickup_ready', internal_order_id)

    # Notify admins AFTER potentially long QR generation/upload
    # Pass the fresh order_details and the generated code
    send_admin_notification(order_details, verification_code)

    # Send QR code and message to the user
    main_keyboard = get_main_reply_keyboard() # Standard reply keyboard for after order
    if ticket_qr_url:
        logging.info(f"Sending pickup QR URL to user {student_db_id}: {ticket_qr_url}")
        try:
             # Send photo using the URL
             bot.send_photo(student_db_id, ticket_qr_url, caption=pickup_msg,
                           parse_mode='MarkdownV2', reply_markup=main_keyboard)
        except telebot.apihelper.ApiTelegramException as e:
             logging.error(f"Error sending QR photo URL {ticket_qr_url} to {student_db_id}: {e}")
             # Fallback to text message if sending photo URL fails
             send_telegram_message(student_db_id, pickup_msg, parse_mode='MarkdownV2', reply_markup=main_keyboard)
        except Exception as e:
             logging.error(f"Unexpected error sending QR photo {ticket_qr_url} to {student_db_id}: {e}")
             send_telegram_message(student_db_id, pickup_msg, parse_mode='MarkdownV2', reply_markup=main_keyboard)
    else:
        # Fallback message if QR generation/upload failed
        logging.warning(f"QR generation/upload failed for order {internal_order_id}. Sending text fallback.")
        fallback_pickup_msg = (
             f"🎉 *Payment Confirmed\\!* \\(Order \\#{internal_order_id}\\)\n\n"
             f"⚠️ QR Code generation failed\\. Please use the details below for pickup\\.\n\n"
             f"*Verification Code:* `{escape_markdown(verification_code or 'N/A')}`\n"
             f"*Service:* {escape_markdown(service_type)}\n"
             f"*Web Ticket Link:* {link_markdown}\n\n"
             f"Show the Verification Code or Web Ticket at the counter\\.\n"
             f"*Est\\. Prep Time:* ~10\\-15 minutes\\."
        )
        send_telegram_message(student_db_id, fallback_pickup_msg, parse_mode='MarkdownV2', reply_markup=main_keyboard)

    logging.info(f"Payment processing complete for Order ID: {internal_order_id}")


# --- Functions handling specific steps in the ordering flow ---
# (add_item_to_cart_and_prompt, view_archives_command_handler, handle_admin_text_commands,
#  handle_admin_callbacks, prompt_for_phone_number, start_menu_flow, handle_status_check)
# These are mostly unchanged logic-wise from your original code, focusing on DB calls.
# Keep them concise here or ensure they handle potential None returns from db_manager gracefully.

def add_item_to_cart_and_prompt(student_db_id, chat_id, message_id, item_id, quantity):
    """Adds item to cart in DB, updates user, asks Add More/Checkout."""
    current_order_id = db_manager.get_session_order_id(student_db_id)
    item = db_manager.get_menu_item(item_id)

    if not item or current_order_id is None or quantity <= 0:
        logging.warning(f"Error adding item: Item={item_id}, Order={current_order_id}, Qty={quantity}")
        error_msg = "⚠️ Error adding item. Please start over."
        # Try to edit the message, otherwise send a new one
        try:
            bot.edit_message_text(error_msg, chat_id, message_id, reply_markup=None)
            # Send main keyboard separately
            send_telegram_message(chat_id, "Tap 'Menu 🍽️' to try again.", reply_markup=get_main_reply_keyboard())
        except Exception:
            # If edit fails, start_menu_flow sends a new message
            start_menu_flow(student_db_id, chat_id, error_msg=error_msg)
        return

    order_details = db_manager.get_order_details(current_order_id)
    # Handle case where order might have been deleted or is None
    if not order_details:
         logging.warning(f"Order {current_order_id} not found when adding item.")
         start_menu_flow(student_db_id, chat_id, error_msg="⚠️ Order not found. Starting over.")
         return

    current_items = db_manager.parse_order_items(order_details.get('items')) # Use parser
    current_total = order_details.get('total_amount', 0.0)

    # Ensure item price is valid before calculation
    item_price = item.get('price')
    if item_price is None:
         logging.error(f"Item {item_id} ('{item.get('name')}') has invalid price. Cannot add to cart.")
         try: bot.edit_message_text("⚠️ Cannot add item, price is missing.", chat_id, message_id)
         except Exception: send_telegram_message(chat_id, "⚠️ Cannot add item, price is missing.")
         return

    # Check if item already in cart to update quantity? (Optional enhancement)
    # For now, just append
    current_items.append({'id': item['id'], 'name': item['name'], 'price': item_price, 'qty': quantity})
    new_total = current_total + (item_price * quantity)

    # Update DB
    if not db_manager.update_order_cart(current_order_id, current_items, new_total):
        logging.error(f"Failed to update cart for order {current_order_id}.")
        # Inform user and potentially reset?
        try:
             bot.edit_message_text("⚠️ Error saving item to order. Please try again.", chat_id, message_id)
        except Exception: # If edit fails, send new
             send_telegram_message(chat_id, "⚠️ Error saving item to order. Please try again.")
        # Don't proceed without saving
        return

    # Update state
    db_manager.set_session_state(student_db_id, 'awaiting_add_more', current_order_id)

    summary_msg = (
        f"✅ Added *{item['name'].title()} x {quantity}*.\n\n"
        f"💰 *Current Total:* ₹{new_total:.2f}\n\n"
        f"Add more items or proceed?"
    )

    keyboard = get_add_more_inline_keyboard()
    try:
        # Try editing the previous message (e.g., the quantity selection)
        bot.edit_message_text(summary_msg, chat_id, message_id, reply_markup=keyboard, parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        # If edit fails (e.g., message too old, user deleted it), send a new message
        if "message can't be edited" in str(e) or "message to edit not found" in str(e):
             logging.warning(f"Could not edit message {message_id} for add_item prompt. Sending new.")
             send_telegram_message(chat_id, summary_msg, reply_markup=keyboard, parse_mode='Markdown')
        else:
             logging.error(f"Error editing message in add_item_to_cart: {e}")
             # Send new as fallback
             send_telegram_message(chat_id, summary_msg, reply_markup=keyboard, parse_mode='Markdown')

# Archive viewing command REMOVED - pg_cron handles cleanup

def handle_admin_text_commands(msg_text, chat_id):
    """Handle admin text commands for menu management."""
    parts = msg_text.lower().split()
    command = parts[0] if parts else ''
    logging.info(f"Admin command received from {chat_id}: {msg_text}")

    # Helper to send message back to admin
    def reply_admin(text, **kwargs):
        send_admin_message(chat_id, text, **kwargs)

    # --- Menu Management ---
    if command in ['add', 'update', 'delete']:
        if command == 'add' or (command == 'update' and len(parts) > 1 and parts[1] == 'menu'):
            # Syntax: add menu <section> <Item Name> <Price>
            # Example: add menu lunch Veg Biryani 65.50
            if len(parts) < 5:
                reply_admin("❌ Syntax: `add menu <section> <Item Name> <Price>`")
                return
            section = parts[2].lower()
            if section not in MENU_SECTIONS:
                valid_sections = ", ".join(MENU_SECTIONS.keys())
                reply_admin(f"❌ Invalid section '{section}'. Use: {valid_sections}")
                return
            try:
                price = float(parts[-1])
                name = " ".join(parts[3:-1]).title() # Capitalize name
                if price <= 0: raise ValueError("Price must be positive")
            except (ValueError, IndexError):
                reply_admin("❌ Invalid price or name format.")
                return
            result = db_manager.add_menu_item(name, price, section)
            reply_admin(result, parse_mode='Markdown') # Result includes markdown

        elif command == 'update' and len(parts) == 3:
             # Syntax: update <id> <new_price>
             try:
                 item_id = int(parts[1])
                 price = float(parts[2])
                 if price <= 0: raise ValueError("Price must be positive")
             except (ValueError, IndexError):
                 reply_admin("❌ Syntax: `update <ItemID> <NewPrice>`")
                 return
             result = db_manager.update_menu_item(item_id, price)
             reply_admin(result) # Result is plain text

        elif command == 'delete' and len(parts) == 2:
             # Syntax: delete <id>
             try:
                 item_id = int(parts[1])
             except (ValueError, IndexError):
                 reply_admin("❌ Syntax: `delete <ItemID>`")
                 return
             result = db_manager.delete_menu_item(item_id)
             reply_admin(result) # Result is plain text
        else:
             # Catch invalid update/delete formats
             reply_admin("❓ Unknown or invalid command format. See /start or Instructions.")

    # --- Order Viewing ---
    # Simplified: Only show today's orders via command
    elif command in ['/today', 'today', '/orders', 'orders']:
         today_orders = db_manager.get_today_orders()
         if not today_orders:
             reply_admin("📦 No orders placed yet today.")
             return

         orders_text = f"📦 *Today's Orders* ({len(today_orders)} total)\n\n"
         for order in today_orders:
             status = order.get('status', 'N/A')
             status_emoji = {'pending': '🟡', 'payment_pending': '🟠', 'paid': '🟢', 'delivered': '🔵', 'cancelled': '🔴'}.get(status, '⚪')
             order_id = order.get('id', '?')
             total = order.get('total_amount', 0.0)
             # Format time (assuming created_at is datetime object from DictCursor)
             created_at = order.get('created_at')
             time_str = created_at.strftime('%I:%M %p') if created_at else 'N/A'
             items_list = db_manager.parse_order_items(order.get('items', '[]'))
             items_summary = ", ".join([f"{item.get('name', '?').title()} x{item.get('qty', 1)}" for item in items_list[:2]]) # Show first 2 items
             if len(items_list) > 2: items_summary += ", ..."

             # Escape content for MarkdownV2
             orders_text += (
                 f"{status_emoji} *Order \\#{escape_markdown(str(order_id))}* \\({escape_markdown(status.title())}\\)\n"
                 f"  \\- Time: {escape_markdown(time_str)}\n"
                 f"  \\- Total: ₹{escape_markdown(f'{total:.2f}')}\n"
                 f"  \\- Items: {escape_markdown(items_summary)}\n\n"
             )
         reply_admin(orders_text, parse_mode='MarkdownV2')

    else:
        reply_admin("❓ Unknown command. Use `add`, `update`, `delete` for menu, or `/today` for orders.")

# Simplified handle_admin_callbacks for Supabase setup
def handle_admin_callbacks(data, chat_id, message_id):
    """Processes inline buttons clicked by Admin."""
    logging.info(f"Admin callback received: {data} from {chat_id}")
    command_parts = data.split(':')
    command_type = command_parts[0]

    # Helper to edit the message, with fallback to send new
    def edit_or_send(text, **kwargs):
        try:
            bot.edit_message_text(text, chat_id, message_id, **kwargs)
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e):
                pass # Ignore if message hasn't changed
            elif "message to edit not found" in str(e):
                 logging.warning(f"Message {message_id} not found to edit, sending new.")
                 send_admin_message(chat_id, text, **kwargs)
            else:
                logging.warning(f"Editing admin message failed ({e}), sending new.")
                send_admin_message(chat_id, text, **kwargs) # Use send_admin_message for fallback
        except Exception as e:
             logging.error(f"Unexpected error editing admin message: {e}")
             send_admin_message(chat_id, text, **kwargs)

    # Navigation Callbacks
    if data == "admin_dashboard":
        edit_or_send("⚙️ *Admin Dashboard*\nSelect an action:",
                     reply_markup=get_admin_dashboard_keyboard(), parse_mode='Markdown')

    elif data == "admin_orders_dashboard":
         edit_or_send("📦 *Order Management*\nView today's live orders:",
                      reply_markup=get_orders_dashboard_keyboard(), parse_mode='Markdown')

    # Action Callbacks
    elif data == "admin_menu":
        is_admin_check = chat_id in ADMIN_CHAT_IDS # Should always be true here
        menu_text = get_menu_text_with_sections(is_admin=is_admin_check)
        # Add instructions for admin menu view
        menu_text += "\n\n*Use text commands to `add`, `update`, or `delete` items.*"
        back_button = InlineKeyboardMarkup().add(InlineKeyboardButton("↩️ Back to Dashboard", callback_data="admin_dashboard"))
        edit_or_send(menu_text, reply_markup=back_button, parse_mode='Markdown')

    elif data == "admin_stats":
        stats = db_manager.get_order_statistics()
        if stats:
             status_lines = "\n".join([f"- {s.replace('_',' ').title()}: {c}" for s, c in stats.get('status_counts', {}).items()])
             stats_text = (
                 f"📈 *Canteen Statistics*\n\n"
                 f"*Successful Orders (Total):* {stats.get('total_orders', 0)}\n"
                 f"*Total Revenue:* ₹{stats.get('total_revenue', 0.0):.2f}\n"
                 f"*Successful Orders (Today):* {stats.get('today_orders', 0)}\n\n"
                 f"*Orders by Current Status:*\n{status_lines if status_lines else 'No orders found.'}"
             )
        else:
             stats_text = "📈 Error retrieving statistics."
        back_button = InlineKeyboardMarkup().add(InlineKeyboardButton("↩️ Back to Dashboard", callback_data="admin_dashboard"))
        edit_or_send(stats_text, reply_markup=back_button, parse_mode='Markdown')

    elif data == "admin_help":
        help_text = (
             "❓ *Admin Instructions*\n\n"
             "Use text commands to manage the menu:\n\n"
             "1️⃣ *Add/Update Item:*\n"
             "`add menu <section> <Item Name> <Price>`\n"
             "*Example:* `add menu snacks Samosa 15`\n"
             "(Updates price/section if name exists)\n\n"
             "2️⃣ *Update Price by ID (Alternative):*\n"
             "`update <ItemID> <NewPrice>`\n"
             "*Example:* `update 3 12.50`\n\n"
             "3️⃣ *Remove Item (Makes unavailable):*\n"
             "`delete <ItemID>`\n"
             "*Example:* `delete 5`\n\n"
             "*Sections:* `breakfast`, `lunch`, `snacks`\n"
             "*Tip:* Use 'View/Edit Menu' to see Item IDs."
        )
        back_button = InlineKeyboardMarkup().add(InlineKeyboardButton("↩️ Back to Dashboard", callback_data="admin_dashboard"))
        edit_or_send(help_text, reply_markup=back_button, parse_mode='Markdown')

    elif data == "admin_orders_today":
         # Trigger the text command handler to send today's orders as a new message
         handle_admin_text_commands("/today", chat_id)
         # Edit the inline message to just show a confirmation/back button
         back_button = InlineKeyboardMarkup().add(InlineKeyboardButton("↩️ Back to Order Mgmt", callback_data="admin_orders_dashboard"))
         edit_or_send("✅ Today's orders sent above.", reply_markup=back_button)

    # Order Action Callbacks (Mark as Delivered)
    elif command_type == "delivered":
        try:
            order_id = int(command_parts[1])
            success = db_manager.update_order_status(order_id, 'delivered')
            if success:
                # Edit the original admin notification message to show "DELIVERED" and remove button
                # Fetch original message content or create a simplified delivered message
                order_details = db_manager.get_order_details(order_id) # Fetch details again for confirmation
                if order_details:
                     student_chat_id = order_details.get('student_phone', 'N/A')
                     collected_phone = db_manager.get_user_phone(student_chat_id)
                     display_identifier = collected_phone or student_chat_id
                     total_amount = order_details.get("total_amount")
                     total_amount_str = f"{total_amount:.2f}" if total_amount is not None else "N/A"

                     # Simplified confirmation message in the edited notification
                     delivered_text = (
                          f"🔵 *ORDER DELIVERED* 🔵\n\n"
                          f"🆔 *Order ID:* \\#{escape_markdown(str(order_id))}\n"
                          f"👤 *Customer:* `{escape_markdown(display_identifier)}`\n"
                          f"💰 *Total:* ₹{escape_markdown(total_amount_str)}\n"
                          f"Marked as delivered by admin\\."
                     )
                     edit_or_send(delivered_text, reply_markup=None, parse_mode='MarkdownV2')
                else:
                     # Fallback if order details couldn't be fetched
                     edit_or_send(f"✅ Order #{order_id} marked as DELIVERED.", reply_markup=None)
            else:
                edit_or_send(f"⚠️ Failed to mark order #{order_id} as delivered.", reply_markup=None)
        except (IndexError, ValueError):
            logging.error(f"Invalid 'delivered' callback data: {data}")
            edit_or_send("❌ Error processing delivery confirmation.", reply_markup=None)
        except Exception as e:
             logging.error(f"Error handling 'delivered' callback: {e}")
             edit_or_send("❌ Error processing delivery confirmation.", reply_markup=None)

    # --- Add handling for user-side callbacks transferred here ---
    elif data == 'cancel_order':
        current_order_id = db_manager.get_session_order_id(str(chat_id)) # Use chat_id as user_id
        if current_order_id:
             db_manager.update_order_status(current_order_id, 'cancelled')
             # Reset user state
             db_manager.set_session_state(str(chat_id), 'initial', None)
             logging.info(f"Order {current_order_id} cancelled by user {chat_id}.")
             edit_or_send("❌ Order cancelled.", reply_markup=None)
             # Send the main keyboard again via a new message
             send_telegram_message(chat_id, "Tap 'Menu 🍽️' to start a new order.", reply_markup=get_main_reply_keyboard())
        else:
             # If no active order, just clear the inline keyboard
             edit_or_send("No active order to cancel.", reply_markup=None)

    elif data.startswith('copy_razorpay_'):
        # Pass the full call object to potentially answer callback query later
        handle_copy_razorpay(data, call) # Use dedicated handler

    # ... [Rest of user-side ordering callbacks: item:, qty:, type_qty:, add_more, checkout, service:, confirm_pay] ...
    # These should largely remain the same, just ensure they call the correct db_manager functions
    # and handle potential errors or None returns gracefully.

    # Example: confirm_pay needs error handling for Razorpay link generation
    elif data == 'confirm_pay':
        student_db_id = str(chat_id)
        current_order_id = db_manager.get_session_order_id(student_db_id)
        order = db_manager.get_order_details(current_order_id) if current_order_id else None

        if not order:
            logging.warning(f"confirm_pay: Order {current_order_id} not found for user {student_db_id}.")
            edit_or_send("❌ Order not found. Please start over.", reply_markup=None)
            send_telegram_message(chat_id, "Tap 'Menu 🍽️' to begin.", reply_markup=get_main_reply_keyboard())
            return

        # Check if phone number exists before payment
        if not db_manager.get_user_phone(student_db_id):
            logging.info(f"confirm_pay: Phone number missing for user {student_db_id}. Prompting.")
            db_manager.set_session_state(student_db_id, 'awaiting_phone_number', current_order_id)
            prompt_for_phone_number(student_db_id, chat_id)
            # Edit the confirmation message to indicate waiting for phone
            edit_or_send("📞 Phone number required. Please provide it below.", reply_markup=None)
            return

        # Proceed with payment link generation
        total_amount = order.get('total_amount', 0.0)
        razorpay_order_id = order.get('razorpay_order_id')
        payment_link = order.get('payment_link')

        # Edit message to show processing state
        edit_or_send("⏳ Generating payment link...", reply_markup=None)

        # Generate link IF it doesn't exist already
        if not (razorpay_order_id and payment_link):
            logging.info(f"Generating new Razorpay link for order {current_order_id}.")
            try:
                razorpay_order_id, payment_link = generate_razorpay_payment_link(current_order_id, total_amount, student_db_id)
                if razorpay_order_id and payment_link:
                    db_manager.update_razorpay_details(current_order_id, razorpay_order_id, payment_link)
                else:
                     raise ValueError("Razorpay link generation returned None") # Trigger except block

            except (razorpay.errors.BadRequestError, requests.exceptions.ConnectionError, ValueError) as e:
                logging.error(f"Failed to generate Razorpay link for order {current_order_id}: {e}")
                db_manager.set_session_state(student_db_id, 'initial', None) # Reset state on critical error
                send_telegram_message(chat_id,
                                      "❌ Error creating payment link. Please try creating a new order.",
                                      reply_markup=get_main_reply_keyboard())
                return # Stop processing
            except Exception as e:
                 logging.error(f"Unexpected error during Razorpay link gen: {e}")
                 db_manager.set_session_state(student_db_id, 'initial', None)
                 send_telegram_message(chat_id,
                                       "❌ An unexpected error occurred. Please try creating a new order.",
                                       reply_markup=get_main_reply_keyboard())
                 return # Stop processing
        else:
            logging.info(f"Reusing existing Razorpay link for order {current_order_id}.")

        # If link exists (either old or newly generated)
        if payment_link:
            db_manager.update_order_status(current_order_id, 'payment_pending')
            payment_keyboard = create_payment_keyboard(payment_link, current_order_id)
            payment_qr_url = generate_payment_qr_code(payment_link, current_order_id)

            total_amount_str = f"{total_amount:.2f}" if total_amount is not None else "N/A"
            payment_msg = (
                f"✅ *Order Ready for Payment* \\(ID: \\#{current_order_id}\\)\n\n"
                f"💰 *Total:* ₹{escape_markdown(total_amount_str)}\n\n"
                f"💳 Please pay using the button or QR code below\\.\n"
                f"_(Status updates automatically after payment)_"
            )

            # Send payment details (QR + Button) as a new message
            if payment_qr_url and payment_keyboard:
                 try:
                     bot.send_photo(chat_id, payment_qr_url, caption=payment_msg,
                                    parse_mode='MarkdownV2', reply_markup=payment_keyboard)
                 except Exception as e:
                      logging.error(f"Error sending payment QR photo {payment_qr_url}: {e}")
                      # Fallback to text + button if photo send fails
                      send_telegram_message(chat_id, payment_msg, parse_mode='MarkdownV2', reply_markup=payment_keyboard)
            elif payment_keyboard: # If QR failed but button okay
                 send_telegram_message(chat_id, payment_msg + "\n\n_(QR code generation failed)_",
                                       parse_mode='MarkdownV2', reply_markup=payment_keyboard)
            else: # If both failed
                 send_telegram_message(chat_id, "❌ Error preparing payment options. Please try again.",
                                       reply_markup=get_main_reply_keyboard())

            db_manager.set_session_state(student_db_id, 'waiting_for_payment', current_order_id)
        else:
             # Should not happen if generation succeeded/reused, but as a safeguard
             logging.error(f"Payment link is unexpectedly missing for order {current_order_id} after generation/reuse attempt.")
             send_telegram_message(chat_id, "❌ Critical error retrieving payment link. Please start over.",
                                    reply_markup=get_main_reply_keyboard())
             db_manager.set_session_state(student_db_id, 'initial', None)

    # --- Fallback for unknown callbacks ---
    else:
        logging.warning(f"Unhandled callback data received: {data}")
        # Optionally answer the callback to remove the loading state
        try:
             # bot.answer_callback_query(call.id, text="Action not recognized.") # Already answered
             pass
        except Exception:
             pass

# --- Dedicated handler for copy razorpay link ---
def handle_copy_razorpay(data, call):
    """Handles the copy_razorpay callback separately."""
    chat_id = call.message.chat.id
    try:
        order_id = int(data.split('_')[-1])
        order_details = db_manager.get_order_details(order_id)
        if order_details and order_details.get('payment_link'):
            link = order_details['payment_link']
            amount = order_details.get('total_amount')
            amount_str = f"{amount:.2f}" if amount is not None else "N/A"
            copy_msg = (
                f"📋 *Payment Link for Order \\#{order_id}*:\n\n"
                f"`{escape_markdown(link)}`\n\n"
                f"*Amount:* ₹{escape_markdown(amount_str)}"
            )
            # Send as a new message, don't edit the payment prompt
            send_telegram_message(chat_id, copy_msg, parse_mode='MarkdownV2')
            # Answer callbackquery to remove the "loading" state on the button
            bot.answer_callback_query(call.id, text="Link sent as a new message.")
        else:
             bot.answer_callback_query(call.id, text="Payment link not found.", show_alert=True)
    except (IndexError, ValueError):
         logging.error(f"Invalid copy link callback data: {data}")
         bot.answer_callback_query(call.id, text="Error copying link.", show_alert=True)
    except Exception as e:
         logging.error(f"Unexpected error in handle_copy_razorpay: {e}")
         bot.answer_callback_query(call.id, text="Internal error.", show_alert=True)


# ... (rest of the ordering flow handlers: prompt_for_phone_number, start_menu_flow, handle_status_check) ...
# Ensure these are robust against errors from db_manager or Telegram API
def prompt_for_phone_number(student_db_id, chat_id):
    """Sends message prompting user to type their phone number."""
    logging.info(f"Prompting user {student_db_id} for phone number.")
    # Ensure state is correctly set before sending prompt
    db_manager.set_session_state(student_db_id, 'awaiting_phone_number', db_manager.get_session_order_id(student_db_id))

    msg = (
        "📞 **Contact Number Needed**\n\n"
        "Please *type your mobile number* (e.g., `9876543210` or `+919876543210`) so we can finalize your order."
        "\n_(Tap 'Cancel Order' below if you don't wish to proceed)_"
    )
    # Use the specific reply keyboard for phone entry
    send_telegram_message(chat_id, msg, parse_mode='Markdown', reply_markup=get_phone_entry_keyboard())

def start_menu_flow(student_db_id, chat_id, message_id=None, error_msg=None):
    """Initiates or restarts the menu selection flow."""
    logging.info(f"Starting menu flow for user {student_db_id}. message_id={message_id}")
    current_order_id = db_manager.get_session_order_id(student_db_id)

    # Check admin status correctly
    try:
        is_admin = int(student_db_id) in ADMIN_CHAT_IDS
    except ValueError:
        is_admin = False # Treat non-integer IDs as non-admin


    # Check if current order is reusable (status 'pending') or create new
    reuse_order = False
    if current_order_id:
        order_details = db_manager.get_order_details(current_order_id)
        if order_details and order_details.get('status') == 'pending':
            reuse_order = True
            logging.info(f"Reusing pending order {current_order_id} for user {student_db_id}.")
        else:
             logging.info(f"Current order {current_order_id} is not pending ({order_details.get('status') if order_details else 'not found'}). Creating new order.")

    if not reuse_order:
        new_order_id = db_manager.create_order(student_db_id, [], 0.0, 'pending')
        if new_order_id is None:
            logging.error(f"Failed to create new order for user {student_db_id}.")
            send_telegram_message(chat_id, "❌ Error starting a new order. Please try again later.",
                                 reply_markup=get_main_reply_keyboard())
            # Reset state just in case
            db_manager.set_session_state(student_db_id, 'initial', None)
            return
        current_order_id = new_order_id
        # Update session immediately with the new order ID
        db_manager.set_session_state(student_db_id, 'ordering_item', current_order_id)

    # Update state (or re-update if reusing order)
    db_manager.set_session_state(student_db_id, 'ordering_item', current_order_id)

    # Get menu text
    main_message = get_menu_text_with_sections(is_admin=is_admin)
    if error_msg:
        main_message = f"{error_msg}\n\n{main_message}"

    # Get the appropriate keyboard
    menu_keyboard = get_menu_inline_keyboard(student_db_id)

    # Send or Edit message
    if message_id:
        try:
            bot.edit_message_text(main_message, chat_id, message_id,
                                  reply_markup=menu_keyboard, parse_mode='Markdown')
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e) and "message can't be edited" not in str(e):
                 logging.warning(f"Editing menu message {message_id} failed ({e}), sending new.")
                 send_telegram_message(chat_id, main_message, reply_markup=menu_keyboard, parse_mode='Markdown')
            # else ignore "not modified" or "can't be edited" errors
        except Exception as e:
             logging.error(f"Unexpected error editing menu message {message_id}: {e}")
             send_telegram_message(chat_id, main_message, reply_markup=menu_keyboard, parse_mode='Markdown')
    else:
        # Send as a new message if no message_id provided
        send_telegram_message(chat_id, main_message, reply_markup=menu_keyboard, parse_mode='Markdown')

def handle_status_check(student_db_id, chat_id):
    """Handles the 'Order Status 📊' button press."""
    logging.info(f"Handling status check for user {student_db_id}")
    current_order_id = db_manager.get_session_order_id(student_db_id)

    if not current_order_id:
        send_telegram_message(chat_id, "📊 No active order found. Tap 'Menu 🍽️' to start one.",
                             reply_markup=get_main_reply_keyboard())
        return

    order_details = db_manager.get_order_details(current_order_id)

    if not order_details:
        send_telegram_message(chat_id, f"📊 Could not retrieve details for order #{current_order_id}. It might be old or cancelled.",
                              reply_markup=get_main_reply_keyboard())
        # Optionally reset state if order is gone
        # db_manager.set_session_state(student_db_id, 'initial', None)
        return

    status = order_details.get('status', 'N/A').replace('_', ' ').title()
    total = order_details.get('total_amount', 0.0)
    service_type = order_details.get('service_type', 'N/A').replace('_', ' ').title()
    pickup_code = order_details.get('pickup_code', 'Not yet generated')
    items_list = db_manager.parse_order_items(order_details.get('items'))
    items_summary = "\n".join([f"- {item.get('name', '?').title()} x {item.get('qty', 1)}" for item in items_list]) if items_list else "No items recorded"
    
    total_str = f"{total:.2f}" if total is not None else "N/A"

    # Use MarkdownV2 for status message, requires escaping
    status_msg = (
        f"📊 *Order Status* \\(ID: \\#{escape_markdown(str(current_order_id))}\\)\n\n"
        f"*Status:* {escape_markdown(status)}\n"
        f"*Total:* ₹{escape_markdown(total_str)}\n"
        f"*Service:* {escape_markdown(service_type)}\n"
        f"*Pickup Code:* `{escape_markdown(pickup_code)}`\n\n"
        f"*Items:*\n{escape_markdown(items_summary)}"
    )

    send_telegram_message(chat_id, status_msg, parse_mode='MarkdownV2', reply_markup=get_main_reply_keyboard())


# --- TELEGRAM BOT HANDLERS ---

@bot.message_handler(commands=['start'])
def send_welcome(message: Message):
    # --- ADDED GRANULAR LOGGING ---
    chat_id = message.chat.id
    user_id = str(chat_id)
    logging.info(f"[send_welcome] /start received from user {user_id}")

    try:
        is_admin = chat_id in ADMIN_CHAT_IDS
        logging.info(f"[send_welcome] User {user_id} is_admin: {is_admin}")

        # Check time availability only for non-admins
        if not is_admin and not is_bot_available_now():
            logging.info(f"[send_welcome] User {user_id} outside operating hours. Sending unavailable message.")
            unavailable_message(chat_id)
            return

        logging.info(f"[send_welcome] Checking current order state for user {user_id}.")
        # Initialize or update user state
        current_order_id = db_manager.get_session_order_id(user_id)
        logging.info(f"[send_welcome] Found current_order_id: {current_order_id}")
        reset_state = True
        if current_order_id:
             logging.info(f"[send_welcome] Fetching details for order {current_order_id}.")
             order_details = db_manager.get_order_details(current_order_id)
             if order_details and order_details.get('status') in ['payment_pending', 'paid', 'pickup_ready']:
                  reset_state = False
                  logging.info(f"[send_welcome] User {user_id} has active order {current_order_id} ({order_details.get('status')}). Not resetting state.")
             else:
                  logging.info(f"[send_welcome] Order {current_order_id} is not active ({order_details.get('status') if order_details else 'not found'}). Will reset state.")

        if reset_state:
             logging.info(f"[send_welcome] Resetting state to 'initial' for user {user_id}.")
             db_manager.set_session_state(user_id, 'initial', None)
        else:
             current_state = db_manager.get_session_state(user_id)
             logging.info(f"[send_welcome] Updating last_active for user {user_id} with state '{current_state}'.")
             db_manager.set_session_state(user_id, current_state, current_order_id)

        logging.info(f"[send_welcome] Preparing welcome message and keyboard for user {user_id}.")
        if is_admin:
            reply_markup = get_admin_reply_keyboard()
            welcome_msg = "👋 Hello Admin! Use the panel or text commands."
        else:
            reply_markup = get_main_reply_keyboard()
            welcome_msg = ("👋 Welcome to the *Digital Canteen Bot*!\n\n"
                           "Tap *Menu 🍽️* to see items and place an order.")

        logging.info(f"[send_welcome] Sending welcome message to user {user_id}.")
        # Use the function that includes detailed logging
        success = send_telegram_message(chat_id, welcome_msg, parse_mode='Markdown', reply_markup=reply_markup)
        if success:
            logging.info(f"[send_welcome] Welcome message sent successfully to {user_id}.")
        else:
             logging.error(f"[send_welcome] Failed to send welcome message to {user_id}.")

    except Exception as e:
         # Log any unexpected errors within send_welcome
         logging.error(f"[send_welcome] Unexpected error processing /start for user {user_id}: {e}")
         traceback.print_exc()
         # Try sending a generic error message
         send_telegram_message(chat_id, "Sorry, an error occurred. Please try /start again.")
    # --- END ADDED LOGGING ---


@bot.message_handler(func=lambda message: message.text in ['Menu 🍽️', 'Order Status 📊', 'Admin Panel ⚙️', 'Orders 📦'])
def handle_reply_keyboard_buttons(message: Message):
    chat_id = message.chat.id
    user_id = str(chat_id)
    text = message.text.split(' ')[0] # Get the command part
    logging.info(f"Reply keyboard button '{text}' tapped by user {user_id}")

    try:
        is_admin = chat_id in ADMIN_CHAT_IDS

        # Check time for non-admin ordering actions
        if not is_admin and text in ['Menu'] and not is_bot_available_now():
            unavailable_message(chat_id)
            return

        # Admin actions
        if is_admin:
            if text == 'Admin':
                # Send the inline dashboard as a new message
                logging.info(f"[handle_reply_keyboard] Admin {user_id} opening Admin Panel.")
                send_admin_message(chat_id, "⚙️ *Admin Dashboard*\nSelect an action:",
                                  reply_markup=get_admin_dashboard_keyboard(), parse_mode='Markdown')
                return
            elif text == 'Orders':
                # Send the orders dashboard as a new message
                logging.info(f"[handle_reply_keyboard] Admin {user_id} opening Orders Panel.")
                send_admin_message(chat_id, "📦 *Order Management*\nView today's live orders:",
                                   reply_markup=get_orders_dashboard_keyboard(), parse_mode='Markdown')
                return

        # User actions (or Admin using User buttons)
        if text == 'Menu':
            logging.info(f"[handle_reply_keyboard] User {user_id} tapping 'Menu'.")
            current_state = db_manager.get_session_state(user_id)
            # Prevent starting menu if waiting for typed input
            if current_state.startswith(('awaiting_typed_quantity_', 'awaiting_phone_number')):
                 logging.warning(f"[handle_reply_keyboard] User {user_id} in state '{current_state}', blocking menu start.")
                 send_telegram_message(chat_id, "⚠️ Please complete the current step (type quantity/phone) or cancel first.")
                 return
            start_menu_flow(user_id, chat_id) # Start the inline menu flow

        elif text == 'Order': # Corresponds to 'Order Status 📊'
            logging.info(f"[handle_reply_keyboard] User {user_id} tapping 'Order Status'.")
            handle_status_check(user_id, chat_id)
    
    except Exception as e:
        logging.error(f"[handle_reply_keyboard] Unexpected error processing button '{text}' for user {user_id}: {e}")
        traceback.print_exc()
        send_telegram_message(chat_id, "Sorry, an error occurred. Please try /start again.")


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    """Handles all inline button presses."""
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    message_id = call.message.message_id
    data = call.data
    logging.info(f"Callback query received: Data='{data}' from user {user_id}")

    # Acknowledge callback immediately to remove loading state on button
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
         logging.warning(f"Could not answer callback query {call.id}: {e}")


    is_admin = chat_id in ADMIN_CHAT_IDS
    is_admin_action = data.startswith(('admin_', 'delivered:')) # Define admin-specific callback prefixes

    # Check time restrictions for non-admin ordering actions via callbacks
    is_ordering_action = data.startswith(('item:', 'qty:', 'type_qty:', 'service:', 'confirm_pay', 'add_more', 'checkout'))
    if not is_admin and not is_admin_action and is_ordering_action and not is_bot_available_now():
         unavailable_message(chat_id)
         # Answer callback again, maybe with an alert?
         try: bot.answer_callback_query(call.id, text="Bot is currently unavailable.", show_alert=True)
         except Exception: pass
         return

    # Route to appropriate handler
    if is_admin and is_admin_action:
        handle_admin_callbacks(data, chat_id, message_id)
    else:
        # Handle user ordering flow and general callbacks (like cancel, copy)
        # Ensure user callbacks also handle potential exceptions gracefully
        try:
            handle_user_callbacks(call) # Use the separate function
        except Exception as e:
             logging.error(f"Error handling user callback {data} for user {user_id}: {e}")
             traceback.print_exc()
             # Try to send a generic error message to the user
             try:
                 # Attempt to edit the message where the button was pressed
                 bot.edit_message_text("❌ An unexpected error occurred. Please try starting over.", chat_id, message_id, reply_markup=None)
                 send_telegram_message(chat_id, "Tap 'Menu 🍽️' to begin again.", reply_markup=get_main_reply_keyboard())
             except Exception:
                 # If editing fails, send a new message
                 send_telegram_message(chat_id, "❌ An unexpected error occurred. Tap 'Menu 🍽️' to start over.", reply_markup=get_main_reply_keyboard())


# NEW function to handle user-specific and general callbacks cleanly
def handle_user_callbacks(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    message_id = call.message.message_id
    data = call.data

    # --- Ordering Flow Callbacks ---
    current_order_id = db_manager.get_session_order_id(user_id)

    if data.startswith('item:'):
        try:
            item_id = int(data.split(':')[1])
            item = db_manager.get_menu_item(item_id)
            if item and current_order_id is not None:
                # Prompt for quantity
                price = item.get('price')
                price_str = f"{price:.2f}" if price is not None else "N/A"
                text = f"Selected *{item.get('name', 'N/A').title()}* (₹{price_str}). How many?"
                keyboard = get_quantity_inline_keyboard(item_id)
                bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard, parse_mode='Markdown')
                db_manager.set_session_state(user_id, f'selecting_quantity_{item_id}', current_order_id) # Update state
            else:
                raise ValueError("Item or order not found")
        except (IndexError, ValueError, TypeError) as e:
             logging.error(f"Error processing item callback {data}: {e}")
             bot.edit_message_text("❌ Error selecting item. Please start over.", chat_id, message_id, reply_markup=None)
             start_menu_flow(user_id, chat_id) # Restart flow

    elif data.startswith('qty:'):
        try:
            _, item_id_str, qty_str = data.split(':')
            item_id = int(item_id_str)
            quantity = int(qty_str)
            if quantity <= 0: raise ValueError("Quantity must be positive")
            # Add to cart (this function now handles editing/sending the next step message)
            add_item_to_cart_and_prompt(user_id, chat_id, message_id, item_id, quantity)
        except (IndexError, ValueError, TypeError) as e:
             logging.error(f"Error processing quantity callback {data}: {e}")
             item_id_fallback = data.split(':')[1] if len(data.split(':')) > 1 else None
             fallback_keyboard = get_quantity_inline_keyboard(item_id_fallback) if item_id_fallback else None
             bot.edit_message_text("❌ Invalid quantity selection. Please try again.", chat_id, message_id, reply_markup=fallback_keyboard)


    elif data.startswith('type_qty:'):
        try:
            item_id = int(data.split(':')[1])
            item = db_manager.get_menu_item(item_id)
            if item and current_order_id is not None:
                 db_manager.set_session_state(user_id, f'awaiting_typed_quantity_{item_id}', current_order_id)
                 text = f"✍️ Please *type the quantity* for *{item.get('name', 'N/A').title()}*:"
                 # Provide a cancel/back option
                 back_key = InlineKeyboardMarkup().add(InlineKeyboardButton("↩️ Back to Menu", callback_data="menu_start"))
                 bot.edit_message_text(text, chat_id, message_id, reply_markup=back_key, parse_mode='Markdown')
            else:
                 raise ValueError("Item or order not found")
        except (IndexError, ValueError, TypeError) as e:
             logging.error(f"Error processing type_qty callback {data}: {e}")
             bot.edit_message_text("❌ Error. Please start over.", chat_id, message_id, reply_markup=None)
             start_menu_flow(user_id, chat_id)

    elif data == 'add_more':
        # Simply go back to the main menu selection
        start_menu_flow(user_id, chat_id, message_id) # Edit the current message to show menu

    elif data == 'checkout':
        if current_order_id and db_manager.get_order_details(current_order_id):
             db_manager.set_session_state(user_id, 'awaiting_service_type', current_order_id)
             bot.edit_message_text("🍴 *Checkout:* Choose service type:", chat_id, message_id,
                                   reply_markup=get_service_type_inline_keyboard(), parse_mode='Markdown')
        else:
             bot.edit_message_text("🛒 Your cart is empty or order expired. Please start over.", chat_id, message_id, reply_markup=None)
             start_menu_flow(user_id, chat_id)

    elif data.startswith('service:'):
        try:
            service_type = data.split(':')[1]
            if current_order_id and service_type in ['dine_in', 'parcel']:
                 if db_manager.update_order_service_type(current_order_id, service_type):
                      order = db_manager.get_order_details(current_order_id)
                      if not order: raise ValueError("Order disappeared after service type update")

                      # Check for phone number BEFORE showing final confirmation
                      if not db_manager.get_user_phone(user_id):
                           db_manager.set_session_state(user_id, 'awaiting_phone_number', current_order_id)
                           prompt_for_phone_number(user_id, chat_id)
                           # Edit the service type message to indicate waiting for phone
                           bot.edit_message_text("📞 Phone number needed.", chat_id, message_id, reply_markup=None)
                           return # Stop here, wait for phone input

                      # Phone number exists, show final confirmation
                      items_list = db_manager.parse_order_items(order.get('items'))
                      food_summary = "\n".join([f"• {item.get('name', '?').title()} x {item.get('qty', 1)} (₹{item.get('price', 0.0):.2f})" for item in items_list])
                      contact = db_manager.get_user_phone(user_id) # Fetch again to be sure
                      total_amount = order.get("total_amount")
                      total_amount_str = f"{total_amount:.2f}" if total_amount is not None else "N/A"


                      confirmation_msg = (
                           f"📝 *Final Confirmation* \\(Order \\#{current_order_id}\\)\n\n"
                           f"*Contact:* `{escape_markdown(contact or 'Not Provided')}`\n"
                           f"*Service:* {escape_markdown(service_type.replace('_', ' ').title())}\n"
                           f"*Total:* ₹{escape_markdown(total_amount_str)}\n\n"
                           f"*Items:*\n{food_summary}\n\n"
                           f"Confirm to proceed to payment\\."
                      )
                      db_manager.set_session_state(user_id, 'confirming_order', current_order_id)
                      bot.edit_message_text(confirmation_msg, chat_id, message_id,
                                            reply_markup=get_confirmation_inline_keyboard(), parse_mode='MarkdownV2')
                 else:
                      raise ValueError("Failed to update service type in DB")
            else:
                 raise ValueError("Invalid service type or missing order ID")
        except (IndexError, ValueError, TypeError) as e:
             logging.error(f"Error processing service type callback {data}: {e}")
             bot.edit_message_text("❌ Error setting service type. Please start over.", chat_id, message_id, reply_markup=None)
             start_menu_flow(user_id, chat_id)

    # --- General Callbacks (Cancel, Copy Link, Back to Menu) ---
    elif data == 'menu_start': # Back to menu from quantity etc.
        start_menu_flow(user_id, chat_id, message_id)

    elif data == 'cancel_order':
        # This can be triggered by admin or user, logic is the same
        handle_admin_callbacks(data, chat_id, message_id) # Reuse admin logic

    elif data.startswith('copy_razorpay_'):
        # This can be triggered by admin or user
        handle_copy_razorpay(data, call) # Pass the full call object

    elif data == 'confirm_pay':
         # This crucial step involves external calls and state changes
         handle_admin_callbacks(data, chat_id, message_id) # Reuse admin logic

    else:
        logging.warning(f"Unhandled user callback data received: {data}")
        # Optionally answer callback if not already done
        try: bot.answer_callback_query(call.id, text="Action not recognized.")
        except Exception: pass


@bot.message_handler(content_types=['text'])
def handle_text_messages(message: Message):
    """Handles typed text: admin commands, phone numbers, quantities."""
    chat_id = message.chat.id
    user_id = str(chat_id)
    text = message.text.strip()
    logging.info(f"Text message received from {user_id}: '{text}'")

    is_admin = chat_id in ADMIN_CHAT_IDS

    # 1. Handle Admin Commands first if user is admin
    if is_admin:
        # Check if it's a known reply keyboard button (handled elsewhere)
        if text not in ['Menu 🍽️', 'Order Status 📊', 'Admin Panel ⚙️', 'Orders 📦']:
             # Assume it's a text command for menu management or order viewing
             handle_admin_text_commands(text, chat_id)
             return # Stop further processing if it was an admin command

    # 2. Check time availability for non-admins for subsequent actions
    if not is_admin and not is_bot_available_now():
        # Don't send unavailable message if they just typed 'Cancel' or something innocuous
        current_state_check = db_manager.get_session_state(user_id)
        if current_state_check != 'initial': # Only block if in an active flow
             unavailable_message(chat_id)
             return

    # 3. Check user state for expected inputs (phone, quantity)
    current_state = db_manager.get_session_state(user_id)
    current_order_id = db_manager.get_session_order_id(user_id)

    # Handle 'Cancel Order ❌' text input from reply keyboard
    if text == 'Cancel Order ❌':
         if current_order_id:
             db_manager.update_order_status(current_order_id, 'cancelled')
             db_manager.set_session_state(user_id, 'initial', None)
             send_telegram_message(chat_id, "❌ Order cancelled.", reply_markup=ReplyKeyboardRemove())
             send_telegram_message(chat_id, "Tap 'Menu 🍽️' to start again.", reply_markup=get_main_reply_keyboard())
         else:
             send_telegram_message(chat_id, "No active order to cancel.", reply_markup=ReplyKeyboardRemove())
             send_telegram_message(chat_id, "Tap 'Menu 🍽️' to start.", reply_markup=get_main_reply_keyboard())
         return

    # Awaiting Phone Number
    if current_state == 'awaiting_phone_number':
        # Basic validation (adjust regex as needed)
        # Allows optional + and requires 7-15 digits
        phone_match = re.fullmatch(r'\+?\d{7,15}', text)
        if not phone_match:
            send_telegram_message(chat_id, "❌ Invalid format. Please enter phone number (e.g., `+919876543210` or `9876543210`).", parse_mode='Markdown')
            # Re-send prompt with keyboard
            prompt_for_phone_number(user_id, chat_id)
            return

        phone_number = text
        logging.info(f"Phone number '{phone_number}' received from user {user_id}.")
        db_manager.update_user_phone(user_id, phone_number)

        # Remove the reply keyboard immediately
        send_telegram_message(chat_id, f"✅ Phone number saved: {phone_number}", reply_markup=ReplyKeyboardRemove())

        # CRITICAL: Now trigger the final confirmation display again
        # Fetch the order details needed for confirmation
        order = db_manager.get_order_details(current_order_id)
        if order:
             service_type = order.get('service_type', 'N/A')
             items_list = db_manager.parse_order_items(order.get('items'))
             food_summary = "\n".join([f"• {item.get('name', '?').title()} x {item.get('qty', 1)} (₹{item.get('price', 0.0):.2f})" for item in items_list])
             contact = phone_number # Use the number just entered
             total_amount = order.get("total_amount")
             total_amount_str = f"{total_amount:.2f}" if total_amount is not None else "N/A"

             confirmation_msg = (
                  f"📝 *Final Confirmation* \\(Order \\#{current_order_id}\\)\n\n"
                  f"*Contact:* `{escape_markdown(contact)}`\n"
                  f"*Service:* {escape_markdown(service_type.replace('_', ' ').title())}\n"
                  f"*Total:* ₹{escape_markdown(total_amount_str)}\n\n"
                  f"*Items:*\n{food_summary}\n\n"
                  f"Confirm to proceed to payment\\."
             )
             db_manager.set_session_state(user_id, 'confirming_order', current_order_id)
             # Send the final confirmation with inline buttons as a NEW message
             send_telegram_message(chat_id, confirmation_msg,
                                   reply_markup=get_confirmation_inline_keyboard(), parse_mode='MarkdownV2')
        else:
             logging.error(f"Order {current_order_id} not found after phone entry for user {user_id}.")
             send_telegram_message(chat_id, "❌ Error retrieving order details. Please start over.",
                                   reply_markup=get_main_reply_keyboard())
             db_manager.set_session_state(user_id, 'initial', None)
        return

    # Awaiting Typed Quantity
    elif current_state.startswith('awaiting_typed_quantity_'):
        try:
            quantity = int(text)
            if quantity <= 0: raise ValueError("Quantity must be positive.")

            item_id = int(current_state.split('_')[-1]) # Extract item_id from state
            logging.info(f"Typed quantity {quantity} received for item {item_id} from user {user_id}.")

            # Add item to cart - this function sends the next step message
            # Pass message_id=None because we need to send a new message
            # after the user's typed quantity message.
            add_item_to_cart_and_prompt(user_id, chat_id, message_id=None, item_id=item_id, quantity=quantity)

        except (ValueError, IndexError, TypeError):
             logging.warning(f"Invalid typed quantity '{text}' received from user {user_id}.")
             send_telegram_message(chat_id, "❌ Invalid quantity. Please type a whole number greater than 0.")
             # Keep user in the same state to allow re-entry
        return

    # 4. Fallback for unexpected text
    else:
        # Ignore if it looks like a command meant for admin but sent by user
        if not is_admin and text.lower() in ['add', 'update', 'delete', '/today', 'today', '/orders', 'orders']:
             logging.info(f"Ignoring potential admin command '{text}' from non-admin user {user_id}.")
             # Send a generic help message
             main_keyboard = get_main_reply_keyboard()
             send_telegram_message(chat_id, "Please use the buttons below or type /start.", reply_markup=main_keyboard)
             return

        # Default response if text doesn't match any expected input
        logging.info(f"Unhandled text message from {user_id}: '{text}' in state: {current_state}")
        main_keyboard = get_admin_reply_keyboard() if is_admin else get_main_reply_keyboard()
        send_telegram_message(chat_id, "Sorry, I didn't understand that. Please use the buttons or /start.", reply_markup=main_keyboard)


# --- TELEGRAM BOT WEBHOOK ENTRY POINT for Vercel ---

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    """Handles incoming updates from Telegram via webhook."""
    if request.headers.get('content-type') == 'application/json':
        try:
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            logging.info("Webhook received update.") # Basic log

            # --- DEBUGGING VERSION: Process directly ---
            logging.debug("Processing update directly (no thread)...")
            bot.process_new_updates([update])
            logging.debug("Update processing finished.")
            # --- END DEBUGGING VERSION ---

            # --- PRODUCTION VERSION (Use this after debugging) ---
            # logging.debug("Processing update in background thread...")
            # threading.Thread(target=bot.process_new_updates, args=[[update]]).start()
            # logging.debug("Thread started, returning OK to Telegram.")
            # --- END PRODUCTION VERSION ---

            return 'OK', 200 # Always return 200 OK quickly to Telegram

        except json.JSONDecodeError as e:
            logging.error(f"Error decoding JSON from Telegram webhook: {e}")
            return 'Bad Request', 400
        except Exception as e:
            # Catch errors during direct processing (in debugging version)
            logging.error(f"❌ CRASH IN WEBHOOK HANDLER processing update: {e}")
            traceback.print_exc()
            # Return 500 to indicate an error, Telegram might retry
            return 'Internal Server Error', 500
    else:
        logging.warning("Webhook received non-JSON request.")
        return 'Unsupported Media Type', 415

# Vercel's Python runtime will automatically find and serve the 'app' object.
# No app.run() needed.

