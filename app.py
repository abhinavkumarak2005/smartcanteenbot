import os
import db_manager
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
from datetime import datetime, timedelta, timezone  # Import timezone for robust handling
import traceback
import logging
import razorpay
import re
from flask import Flask, request, jsonify, render_template_string
import requests # Ensure requests is imported for exception handling

# --- PROJECT CONFIGURATION & ROBUST .ENV LOADING ---
BASE_DIR = Path(__file__).resolve().parent
DOTENV_PATH = BASE_DIR / '.env'

# Load environment variables using the explicit path
load_dotenv(dotenv_path=DOTENV_PATH)

# --- ENVIRONMENT CHECK AND RAZORPAY SETUP ---

# Required environment variables list
REQUIRED_ENV_VARS = [
    'BOT_TOKEN', 'RAZORPAY_KEY_ID', 'RAZORPAY_KEY_SECRET', 'BOT_PUBLIC_URL'
]

# Check for required variables and exit if missing
for var in REQUIRED_ENV_VARS:
    if not os.getenv(var):
        print(f"❌ ERROR: Missing required environment variable: {var}")
        print("Please check your .env file and ensure all required keys are present and not commented out.")
        exit(1)

# --- CONFIGURATION ---
TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_IDS = [int(num.strip()) for num in os.getenv('ADMIN_CHAT_IDS', '').split(',') if num.strip().isdigit()]
PAYEE_NAME = os.getenv('PAYEE_NAME', 'Canteen Staff')
QR_CODE_BASE_URL = os.getenv('QR_CODE_BASE_URL', 'http://your-public-url/static/')
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
app = Flask(__name__)


# Flask will run in a separate thread/process managed by the main script


# --- FLASK ENDPOINT REGISTRATION FUNCTION (All Web Routes) ---

def setup_flask_routes():
    """Registers all necessary Flask routes to prevent AssertionError."""
    # NOTE: This is called on script load and before main execution.

    @app.route('/order_success', methods=['GET'])
    def order_success():
        """Endpoint for Razorpay redirect after successful payment (browser view)."""
        html_content = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Payment Successful</title>
            <style>
                body { 
                    font-family: sans-serif; 
                    text-align: center; 
                    background-color: #0b1a2e; 
                    color: #e0f2f1; 
                    padding: 50px; 
                }
                .container {
                    background-color: #1a304a;
                    border-radius: 12px;
                    padding: 30px;
                    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2);
                    max-width: 400px;
                    margin: 0 auto;
                }
                h1 { color: #81c784; }
                p { margin-bottom: 20px; }
            </style>
        </head>
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
        """Endpoint for Razorpay to send payment completion notifications."""
        print("🚨 Webhook received from Razorpay.")

        # 1. Get the payload and signature
        payload_bytes = request.get_data()
        signature = request.headers.get('X-Razorpay-Signature')

        # 2. Verify the signature
        try:
            payload_str = payload_bytes.decode('utf-8')
            RAZORPAY_CLIENT.utility.verify_webhook_signature(payload_str, signature, WEBHOOK_SECRET)
            print("✅ Webhook signature verified successfully!")

        except Exception as e:
            print(f"❌ Webhook signature verification failed: {e}")
            return jsonify(success=False,
                           message="Signature verification failed: Secret mismatch or internal library error"), 400

        # 3. Process the event
        try:
            event = json.loads(payload_str)

            if event['event'] == 'payment.captured':
                payment_entity = event['payload']['payment']['entity']
                razorpay_order_id = payment_entity['order_id']
                rzp_order_details = RAZORPAY_CLIENT.order.fetch(razorpay_order_id)

                internal_order_id = rzp_order_details['notes']['internal_order_id']
                student_db_id = rzp_order_details['notes']['telegram_chat_id']

                print(f"✅ Payment captured for RZ ID: {razorpay_order_id}, Internal ID: {internal_order_id}")

                threading.Thread(
                    target=handle_successful_payment,
                    args=(int(internal_order_id), student_db_id)
                ).start()

        except Exception as e:
            print(f"❌ Error processing webhook payload: {e}")
            traceback.print_exc()
            return jsonify(success=False, message="Internal processing error"), 500

        # 4. Return 200 OK to Razorpay immediately
        return jsonify(success=True), 200

    @app.route('/order_display/<int:order_id>/<string:verification_code>', methods=['GET'])
    def order_display(order_id, verification_code):
        """
        NEW ENDPOINT: Displays the digital order ticket in a web browser.
        This page is accessed by scanning the QR code.
        """
        order_details = db_manager.get_order_details(order_id)

        # 1. Basic Security Check
        if not order_details or order_details.get('pickup_code') != verification_code:
            return render_template_string(
                "<h1 style='color:red;'>❌ Invalid Order or Verification Code</h1>"
                "<p>Please ensure you scanned the correct QR code.</p>"
            ), 404

        # 2. Prepare Data
        status = order_details.get('status', 'PENDING').upper()

        # Determine color and status message
        if status == 'PAID' or status == 'PICKUP_READY':
            status_color = '#4CAF50'  # Green
            status_message = 'READY FOR PICKUP'
        elif status == 'DELIVERED':
            status_color = '#2196F3'  # Blue
            status_message = 'COLLECTED'
        elif status == 'CANCELLED' or status == 'EXPIRED':
            status_color = '#F44336'  # Red
            status_message = status
        else:
            status_color = '#FF9800'  # Orange
            status_message = 'PROCESSING'

        items_list = db_manager.parse_order_items(order_details.get('items', '[]'))

        # Format item list into HTML strings
        items_html = ""
        for item in items_list:
            items_html += f"<li>{item['name'].title()} x {item['qty']} (₹{item['price']:.2f})</li>"

        # Get actual phone number for display
        student_chat_id = order_details['student_phone']
        display_contact = db_manager.get_user_phone(student_chat_id) or student_chat_id

        # 3. Render HTML template
        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Order #{order_id} Ticket</title>
            <style>
                body {{ font-family: sans-serif; text-align: center; background-color: #f7f7f7; display: flex; flex-direction: column; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px 0; }}
                .ticket {{ 
                    width: 90%; max-width: 500px; background-color: #fff; border: 3px solid #ccc; 
                    border-radius: 15px; padding: 25px; box-shadow: 0 10px 20px rgba(0,0,0,0.1);
                    text-align: center;
                }}
                h2 {{ color: #0b1a2e; border-bottom: 2px dashed #eee; padding-bottom: 10px; margin-bottom: 20px; }}
                .status-box {{ 
                    background-color: {status_color}; color: white; padding: 10px; border-radius: 8px; 
                    font-weight: bold; margin-bottom: 20px; font-size: 1.1em; text-transform: uppercase;
                }}
                .detail-grid {{ 
                    display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; 
                    text-align: left;
                }}
                .detail-label {{ font-weight: bold; color: #555; }}
                .detail-value {{ font-weight: normal; color: #333; }}
                .items ul {{ list-style-type: none; padding: 0; margin-top: 10px; text-align: left; }}
                .items li {{ 
                    background-color: #f0f0f0; padding: 8px; border-radius: 4px; margin-bottom: 5px; 
                    font-weight: 600; color: #333;
                }}
                .pickup-code {{ 
                    font-size: 2.5em; color: {status_color}; font-weight: 900; margin-top: 15px; 
                    letter-spacing: 2px;
                }}
            </style>
        </head>
        <body>
            <div class="ticket">
                <h2>Canteen Order Ticket #{order_id}</h2>
                <div class="status-box" style="background-color: {status_color};">STATUS: {status_message}</div>

                <div class="detail-grid">
                    <div class="detail-label">Total Amount:</div>
                    <div class="detail-value">₹{order_details['total_amount']:.2f}</div>

                    <div class="detail-label">Service Type:</div>
                    <div class="detail-value">{order_details.get('service_type', 'N/A').replace('_', ' ').title()}</div>

                    <div class="detail-label">Contact:</div>
                    <div class="detail-value">{display_contact}</div>
                </div>

                <div class="items">
                    <strong style="color: #0b1a2e;">Ordered Items:</strong>
                    <ul>{items_html}</ul>
                </div>

                <p style="margin-top: 30px; color: #555;">Please present this code at the counter:</p>
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
    """Creates a Razorpay Payment Link object and returns its details."""
    if not BOT_PUBLIC_URL or not BOT_PUBLIC_URL.startswith('http'):
        print("❌ CRITICAL ERROR: BOT_PUBLIC_URL is missing or invalid.")
        return None, None

    try:
        # Generate a daily-unique reference ID for Razorpay
        today_date_str = datetime.now().strftime('%Y%m%d')
        daily_unique_reference_id = f"CANTN_{today_date_str}_{internal_order_id}"

        notes = {
            "internal_order_id": str(internal_order_id),
            "telegram_chat_id": str(student_phone)
        }

        # Get the collected phone number for Razorpay payment link details
        user_phone = db_manager.get_user_phone(student_phone)
        # Fallback for Razorpay if phone is not collected (Razorpay requires a contact)
        razorpay_contact = user_phone if user_phone else student_phone

        data = {
            "amount": int(amount * 100),  # Amount in paise
            "currency": "INR",
            "accept_partial": False,
            # CRITICAL FIX: Use the daily-unique reference ID
            "reference_id": daily_unique_reference_id,
            "description": f"Canteen Order #{internal_order_id} - {PAYEE_NAME}",
            "customer": {
                # Use the collected phone number if available
                "contact": f"+{razorpay_contact}",
                "name": f"Telegram User {razorpay_contact}"
            },
            "notify": {
                "sms": False,
                "email": False
            },
            "callback_url": f"{BOT_PUBLIC_URL}/order_success",  # Redirects here after payment
            "callback_method": "get",
            "notes": notes  # Pass internal IDs to webhook via order entity
        }

        rzp_link = RAZORPAY_CLIENT.payment_link.create(data)

        if 'id' not in rzp_link:
            raise KeyError(f"Razorpay API response is invalid. Missing 'id'. Response keys: {rzp_link.keys()}")

        payment_url = rzp_link['short_url']
        razorpay_order_id = rzp_link['id']

        print(f"💰 Razorpay Payment Link created: {razorpay_order_id} (Link: {payment_url})")
        return razorpay_order_id, payment_url

    except Exception as e:
        print(f"❌ Error generating Razorpay payment link/order: {e}")
        traceback.print_exc()
        # Check for specific BadRequestError related to reference_id reuse
        if isinstance(e, razorpay.errors.BadRequestError) and "reference_id" in str(e):
             # Re-raise the error so the caller can reset the session
            raise razorpay.errors.BadRequestError(str(e))
        return None, None


# --- PAYMENT & QR CODE UTILITIES ---
def create_payment_keyboard(payment_link, order_id):
    """Create inline keyboard with clickable payment button (Razorpay checkout page)."""
    try:
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.row(InlineKeyboardButton("💳 Pay Securely with Razorpay", url=payment_link))
        keyboard.row(InlineKeyboardButton("📋 Copy Payment Link", callback_data=f"copy_razorpay_{order_id}"))
        return keyboard

    except Exception as e:
        print(f"❌ Error creating payment keyboard: {e}")
        return None


def generate_payment_qr_code(payment_link, order_id):
    """Generate QR code for the Razorpay payment link."""
    try:
        filename = f"razorpay_{order_id}_{uuid.uuid4().hex[:8]}.png"
        static_dir = BASE_DIR / 'static'
        static_dir.mkdir(exist_ok=True, parents=True)
        filepath = static_dir / filename

        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(payment_link)
        qr.make(fit=True)

        qr_img = qr.make_image(fill_color="darkblue", back_color="white")
        qr_img.save(filepath)

        return str(filepath)

    except Exception as e:
        print(f"❌ Error generating payment QR code (runtime error): {e}")
        return None


def generate_pickup_qr_code(order_id, student_phone):
    """
    MODIFIED: Generates pickup QR code that links to the order display webpage.
    """
    try:
        # Generate the unique verification code
        verification_code = f"{order_id}{datetime.now().strftime('%M%S')}"

        # Construct the URL pointing to the Flask web page
        if not BOT_PUBLIC_URL:
            raise ValueError("BOT_PUBLIC_URL environment variable is not set.")

        # The URL contains the order ID and the security code
        web_link = f"{BOT_PUBLIC_URL}/order_display/{order_id}/{verification_code}"

        filename = f"pickup_qr_{order_id}_{uuid.uuid4().hex[:8]}.png"

        static_dir = BASE_DIR / 'static'
        static_dir.mkdir(exist_ok=True, parents=True)
        filepath = static_dir / filename

        # Generate QR code for the URL
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(web_link)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="darkgreen", back_color="white")
        qr_img.save(filepath)

        return str(filepath), verification_code, web_link  # RETURN THE WEB LINK

    except Exception as e:
        print(f"❌ Error generating pickup QR code: {e}")
        traceback.print_exc()
        return None, None, None


# --- KEYBOARD GENERATION FUNCTIONS ---

def get_main_reply_keyboard():
    """Creates the main persistent reply keyboard with Menu and Status buttons."""
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=False)
    btn_menu = KeyboardButton('Menu 🍽️')
    btn_status = KeyboardButton('Order Status 📊')
    markup.add(btn_menu, btn_status)
    return markup


# NEW: Admin Reply Keyboard (Replaces text command input)
def get_admin_reply_keyboard():
    """Creates the persistent reply keyboard for admins."""
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=False)
    btn_admin = KeyboardButton('Admin Panel ⚙️')
    btn_menu = KeyboardButton('Menu 🍽️')
    markup.add(btn_admin, btn_menu)
    return markup


# NEW: Admin Inline Dashboard (Simplified)
def get_admin_dashboard_keyboard():
    """Generates the main inline keyboard for the admin dashboard."""
    markup = InlineKeyboardMarkup(row_width=2)
    # Row 1: View Menu and View Stats
    markup.row(
        InlineKeyboardButton("📋 View Menu", callback_data="admin_menu"),
        InlineKeyboardButton("📈 View Stats", callback_data="admin_stats")
    )
    # Row 2: Instructions (Now the single gateway for all text commands)
    markup.row(InlineKeyboardButton("❓ Instructions", callback_data="admin_help"))
    return markup


def get_menu_inline_keyboard(user_id):
    """Generates the Inline Keyboard for item selection."""
    menu = db_manager.get_menu()
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []

    if menu:
        for item in menu:
            # Use 'item:<item_id>' as callback data
            button_text = f"{item['name'].title()} (₹{item['price']:.2f})"
            buttons.append(InlineKeyboardButton(button_text, callback_data=f"item:{item['id']}"))

    # Add buttons row by row (2 per row)
    for i in range(0, len(buttons), 2):
        row = buttons[i:i + 2]
        markup.row(*row)

    # Add a final row for cancellation/back
    markup.row(InlineKeyboardButton("Cancel Order ❌", callback_data="cancel_order"))

    return markup


# NEW: Plain Text Phone Entry Reply Keyboard
def get_phone_entry_keyboard():
    """Creates a Reply Keyboard instructing user to type their number."""
    # CRITICAL: one_time_keyboard=True ensures the keyboard is replaced after the user types.
    markup = ReplyKeyboardMarkup(row_width=1, resize_keyboard=True, one_time_keyboard=True)
    # We include a generic 'Cancel' button to give the user a way out
    markup.add(KeyboardButton('Cancel Order ❌'))
    return markup


def get_quantity_inline_keyboard(item_id):
    """Generates the Inline Keyboard for quantity selection."""
    markup = InlineKeyboardMarkup(row_width=5)

    # Generate quantity buttons 1 through 5
    qty_buttons = [
        InlineKeyboardButton(str(i), callback_data=f"qty:{item_id}:{i}")
        for i in range(1, 6)
    ]
    markup.row(*qty_buttons)

    # NEW: Add button to allow typing quantity
    markup.row(InlineKeyboardButton("✍️ Type Number ( > 5 )", callback_data=f"type_qty:{item_id}"))

    # Add a 'Back' button
    markup.row(InlineKeyboardButton("↩️ Back to Menu", callback_data="menu_start"))
    return markup


def get_add_more_inline_keyboard():
    """Generates the Inline Keyboard for multi-item selection."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add More Items", callback_data="add_more"),
        InlineKeyboardButton("🛒 Proceed to Checkout", callback_data="checkout")
    )
    return markup


def get_service_type_inline_keyboard():
    """Generates the Inline Keyboard for service type selection."""
    markup = InlineKeyboardMarkup(row_width=2)
    # FIX: Use InlineKeyboardButton for an inline keyboard
    markup.add(
        InlineKeyboardButton("🍴 Dine In", callback_data="service:dine_in"),
        InlineKeyboardButton("📦 Parcel/Takeaway", callback_data="service:parcel")
    )
    return markup


def get_confirmation_inline_keyboard():
    """Generates the Inline Keyboard for final confirmation."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_pay"),
        InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order")
    )
    return markup


# --- UTILITY FUNCTIONS ---

def escape_markdown(text):
    """Escapes special characters in text for Telegram Markdown V2."""
    escape_chars = r'_*`[]()~>#+-=|{}.!'
    return "".join(['\\' + char if char in escape_chars else char for char in text])


def send_admin_message(chat_id, text, parse_mode='MarkdownV2', reply_markup=None):
    """
    Unified function to send messages to admins.
    """
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        print(f"❌ Error sending admin message to {chat_id} (Attempt 1/2, {parse_mode} failed): {e}")
        # Fallback 1: Try with standard Markdown
        try:
            bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=reply_markup)
        except Exception as e2:
            # Fallback 2: Plain text
            print(f"❌ Error sending admin message to {chat_id} (Attempt 2/2, Markdown failed): {e2}")
            # Send as Plain Text
            bot.send_message(chat_id, text, parse_mode=None, reply_markup=None)


def send_admin_notification(order_details, verification_code):
    """Send detailed notification message to all admin chat IDs."""
    if not bot:
        return

    try:
        items_list = db_manager.parse_order_items(order_details['items'])
        service_type = order_details.get('service_type', 'N/A')

        # Use the student_phone value directly (which is the Chat ID)
        student_chat_id = order_details['student_phone']

        # Retrieve the collected phone number for display
        collected_phone = db_manager.get_user_phone(student_chat_id)

        # Determine the identifier to display
        display_identifier = collected_phone if collected_phone else student_chat_id

        # 1. Escape item names and prices for the summary list
        food_summary_lines = []
        for item in items_list:
            item_line = f"• {item['name'].title()} x {item['qty']} (₹{item['price']:.2f})"
            food_summary_lines.append(escape_markdown(item_line))
        food_summary = "\n".join(food_summary_lines)

        # 2. Escape the total amount string directly
        total_amount_escaped = escape_markdown(f"{order_details['total_amount']:.2f}")

        # 3. Create the inline keyboard for delivery confirmation
        delivery_keyboard = InlineKeyboardMarkup(row_width=1)
        delivery_keyboard.add(
            InlineKeyboardButton("✅ Order Delivered", callback_data=f"delivered:{order_details['id']}"))

        # 4. Construct the notification message using MarkdownV2 syntax
        notification_msg = (
            f"🚨 \\*NEW ORDER CONFIRMED \\& PAID\\!\\* 🚨\n\n"
            f"🆔 \\*Order ID:\\* \\#{escape_markdown(str(order_details['id']))}\n"
            f"🔢 \\*Verification Code:\\* `{escape_markdown(str(verification_code))}`\n"
            # FIX: Display phone number if collected, otherwise the Chat ID
            f"📞 \\*Student Phone/ID:\\* `{escape_markdown(display_identifier)}`\n"
            f"💰 \\*Total Amount:\\* ₹{total_amount_escaped}\n"
            f"🪑 \\*Service Type:\\* \\*{escape_markdown(service_type.title())}\\*\n\n"
            f"🍽️ \\*Ordered Items:\\*\n{food_summary}\n\n"
            f"🟢 \\*STATUS:\\* Ready for Preparation\n"
            f"📍 \\*Next Step:\\* Prepare order and press 'Order Delivered' upon pickup\\."
        )

        for admin_id in ADMIN_CHAT_IDS:
            send_admin_message(admin_id, notification_msg, reply_markup=delivery_keyboard)

    except Exception as e:
        print(f"❌ Error in admin notification: {e}")
        traceback.print_exc()


def handle_successful_payment(internal_order_id, student_db_id):
    """Handles all post-payment logic (QR code generation, admin notification)."""

    order_details = db_manager.get_order_details(internal_order_id)
    if not order_details:
        return

    db_manager.update_order_status(internal_order_id, 'paid')

    # --- CHANGED: Call the new QR generation function (uses URL) ---
    ticket_qr_path, verification_code, web_link = generate_pickup_qr_code(
        internal_order_id, student_db_id
    )

    # --- Update order details with pickup code in DB ---
    db_manager.update_order_pickup_code(internal_order_id, verification_code)

    service_type = order_details.get('service_type', 'N/A')

    # --- CHANGED: Updated message for QR code link ---
    # NOTE: We are using parse_mode=None (Plain Text) for the caption to avoid crashing.
    pickup_msg = (
        f"🎉 Payment Confirmed! (Order ID: #{internal_order_id})\n\n"
        f"Here is your Order QR Code for pickup!\n\n"
        f"Verification Code: {verification_code}\n"
        f"Service Type: {service_type.title()}\n\n"
        f"For Pickup:\n"
        f"Scan the QR code below.\n"
        f"(Note: If you see a warning page, please click 'Visit Site'.)\n\n"
        f"Alternative Link: {web_link}"
    )

    db_manager.set_session_state(student_db_id, 'pickup_ready', internal_order_id)
    send_admin_notification(order_details, verification_code)  # Admin still gets notification

    main_keyboard = get_main_reply_keyboard()
    if ticket_qr_path:
        # FIX: Sending the photo with PLAIN TEXT parse_mode=None to avoid Markdown crash (Error 400 fix)
        with open(ticket_qr_path, 'rb') as photo:
            bot.send_photo(student_db_id, photo, caption=pickup_msg, parse_mode='Markdown',
                           reply_markup=main_keyboard)
    else:
        # Fallback if QR image generation fails
        fallback_msg = (
            f"🎉 **Payment Confirmed!**\n\n"
            f"QR Code generation failed. Use the link below:\n\n"
            f"🆔 **Order ID:** #{internal_order_id}\n"
            f"🔢 **Verification Code:** `{verification_code}`\n\n"
            f"Show this verification code at the counter for pickup\n"
            f"⏰ **Ready in:** 10-15 minutes\n\n"
            f"🔗 **Alternative Link:** {web_link}"
        )
        # Using Markdown for fallback message
        bot.send_message(student_db_id, fallback_msg, parse_mode='Markdown', reply_markup=main_keyboard)


def add_item_to_cart_and_prompt(student_db_id, chat_id, message_id, item_id, quantity):
    """
    Adds item to cart, updates DB, and prompts for next action (add more or checkout).
    """

    current_order_id = db_manager.get_session_order_id(student_db_id)
    item = db_manager.get_menu_item(item_id)

    if not item or current_order_id is None or quantity <= 0:
        if message_id:
            start_menu_flow(student_db_id, chat_id, message_id, error_msg="⚠️ Error adding item. Please start over.")
        else:
            start_menu_flow(student_db_id, chat_id, error_msg="⚠️ Error adding item. Please start over.")
        return

    # --- ADD ITEM TO CART LOGIC ---
    total_item_price = item['price'] * quantity
    order_details = db_manager.get_order_details(current_order_id)
    current_items = db_manager.parse_order_items(order_details.get('items')) if order_details else []
    current_total = order_details.get('total_amount', 0.0) if order_details else 0.0

    current_items.append({'id': item['id'], 'name': item['name'], 'price': item['price'], 'qty': quantity})
    new_total = current_total + total_item_price

    # Update the database
    db_manager.update_order_cart(current_order_id, current_items, new_total)

    # Switch to 'add more' state
    db_manager.set_session_state(student_db_id, 'awaiting_add_more', current_order_id)

    summary_msg = (
        f"✅ Added *{item['name'].title()} x {quantity}* to your order (ID: #{current_order_id}).\n\n"
        f"💰 *Current Total:* ₹{new_total:.2f}\n\n"
        f"Do you want to add more items or proceed to checkout?"
    )

    current_state = db_manager.get_session_state(student_db_id)

    if current_state.startswith('awaiting_typed_quantity_'):
        # SEND A NEW MESSAGE
        bot.send_message(
            chat_id=chat_id,
            text=summary_msg,
            parse_mode='Markdown',
            reply_markup=get_add_more_inline_keyboard()
        )
    else:
        # EDIT THE BOT'S PREVIOUS MESSAGE.
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=summary_msg,
                parse_mode='Markdown',
                reply_markup=get_add_more_inline_keyboard()
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message can't be edited" in str(e):
                bot.send_message(
                    chat_id=chat_id,
                    text=summary_msg,
                    parse_mode='Markdown',
                    reply_markup=get_add_more_inline_keyboard()
                )
            else:
                raise


# Handler for the /viewarchive text command to send the inline keyboard
def view_archives_command_handler(chat_id):
    """
    Handles the /viewarchive text command by sending a new message with archive file buttons.
    """
    archive_files = db_manager.get_archive_file_list()

    if not archive_files:
        archive_text = "🗄️ **Archived Orders**\n\nNo archive files found. Run the bot for a day to generate archives."
        bot.send_message(chat_id, archive_text, parse_mode='Markdown')
        return

    archive_text = "🗄️ **Select Archive Date**:\n\n"

    archive_keyboard = InlineKeyboardMarkup(row_width=1)

    for filename in archive_files:
        # Filename format: orders_archived_before_YYYY-MM-DD.json
        date_part_str = filename.replace('orders_archived_before_', '').replace('.json', '')

        try:
            # The archive file is named by the cutoff date (e.g., 2025-10-02)
            cutoff_date = datetime.strptime(date_part_str, '%Y-%m-%d')

            # The data inside the file is from the day *before* the cutoff.
            data_date = cutoff_date - timedelta(days=1)
            display_date = data_date.strftime('%d %b %Y')

            # Button text shows the date of the data contained within the archive
            button_text = f"Orders from {display_date}"

        except ValueError:
            display_date = f"Invalid Date ({date_part_str})"
            button_text = display_date

        # Callback to display the archive contents (handled by handle_admin_callbacks)
        archive_keyboard.add(
            InlineKeyboardButton(button_text, callback_data=f"archive_view_file:{filename}")
        )

    bot.send_message(chat_id, archive_text, parse_mode='Markdown', reply_markup=archive_keyboard)
    return


# Handle Admin Text Commands (for menu management AND viewing orders)
def handle_admin_text_commands(msg, chat_id):
    """Handle admin commands using text input (menu management and viewing orders)."""

    parts = msg.lower().split()
    command = parts[0] if parts else ''

    def send_admin_message_wrapper(text, parse_mode='Markdown', reply_markup=None):
        send_admin_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)

    # --- Menu Management Logic ---
    if command == 'add':
        if len(parts) < 3:
            send_admin_message_wrapper("❌ Invalid format. Use: `add <item name> <price>`")
            return
        try:
            price = float(parts[-1])
            item_name = ' '.join(parts[1:-1])
            result = db_manager.add_menu_item(item_name, price)
            send_admin_message_wrapper(result)
        except ValueError:
            send_admin_message_wrapper("❌ Invalid price format. Please use a number.")

    elif command == 'update':
        if len(parts) != 3:
            send_admin_message_wrapper("❌ Invalid format. Use: `update <id> <price>`")
            return
        try:
            item_id = int(parts[1])
            price = float(parts[2])
            result = db_manager.update_menu_item(item_id, price)
            send_admin_message_wrapper(result)
        except ValueError:
            send_admin_message_wrapper("❌ Invalid ID or price format. Please use numbers.")

    elif command == 'delete':
        if len(parts) != 2:
            send_admin_message_wrapper("❌ Invalid format. Use: `delete <id>`")
            return
        try:
            item_id = int(parts[1])
            result = db_manager.delete_menu_item(item_id)
            send_admin_message_wrapper(result)
        except ValueError:
            send_admin_message_wrapper("❌ Invalid ID format. Please use a whole number.")

    # --- TEXT COMMAND LOGIC for orders ---
    elif command in ['/todayorders', '/today', '/liveorders']:
        today_orders = db_manager.get_today_orders()

        if not today_orders:
            orders_text = "📦 **Today's Orders**\n\nNo orders placed yet today."
        else:
            orders_text = f"📦 **Today's Orders** (Total: {len(today_orders)})\n\n"

            for order in today_orders:
                status_emoji = {
                    'pending': '🟡', 'payment_pending': '🟠', 'paid': '🟢',
                    'cancelled': '🔴', 'expired': '⚫', 'delivered': '🔵'
                }.get(order['status'], '⚪')

                # FIX: Format the timestamp and adjust for IST (UTC + 5.5 hours)
                try:
                    # Parse the database timestamp string
                    created_time = datetime.strptime(order['created_at'].split('.')[0], '%Y-%m-%d %H:%M:%S')

                    # Assume DB stores UTC and convert to IST (UTC + 5 hours 30 minutes)
                    local_time = created_time + timedelta(hours=5, minutes=30)

                    time_part = local_time.strftime('%I:%M %p')
                except Exception:
                    time_part = 'N/A'

                # Check for item summary to display more details
                items_list = db_manager.parse_order_items(order.get('items', '[]'))
                item_summary = ", ".join([f"{item.get('name', 'Item')} x{item.get('qty', 1)}" for item in items_list])

                orders_text += (
                    f"{status_emoji} **Order #{order['id']}**\n"
                    f"  - Status: {order['status'].title()}\n"
                    f"  - Total: ₹{order['total_amount']:.2f}\n"
                    f"  - Items: {item_summary}\n"
                    f"  - Time: {time_part}\n\n"
                )

        send_admin_message_wrapper(orders_text)
        return

    elif command in ['/viewarchive', '/archive', '/history']:
        # Call the dedicated handler for archive viewing. This is now robust.
        view_archives_command_handler(chat_id)
        return


def handle_admin_callbacks(data, chat_id, message_id):
    """Processes inline buttons clicked from the Admin Dashboard."""

    command = data.split('_')[1]
    command_type = data.split(':')[0]

    def edit_message(text, reply_markup=None, parse_mode='Markdown'):
        """
        Attempts to edit the inline message.
        """
        # Helper for going back to the main admin dashboard
        back_to_dashboard_inline = InlineKeyboardMarkup().row(
            InlineKeyboardButton("↩️ Back to Dashboard", callback_data="admin_dashboard")
        )

        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e) or "message can't be edited" in str(e):
                return

            # Fallback: Send the message as a new one
            print(f"⚠️ Edit failed for {command}. Sending new message. Error: {e}")
            try:
                # Send a new message with the content and the correct inline buttons
                bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)

                # Send the dashboard keyboard separately for navigation
                bot.send_message(chat_id, "Please use the dashboard buttons below.",
                                 reply_markup=get_admin_reply_keyboard())
            except Exception as e2:
                bot.send_message(chat_id, f"⚠️ Error. Please use text command directly.\n\n{text}", parse_mode=None)

    # Helper for going back to the main admin dashboard (used below)
    back_to_dashboard = InlineKeyboardMarkup().row(
        InlineKeyboardButton("↩️ Back to Dashboard", callback_data="admin_dashboard")
    )

    # Helper to go back to Order Instructions (used after viewing an archive file)
    back_to_orders_instruct = InlineKeyboardMarkup().row(
        InlineKeyboardButton("↩️ Back to Orders Instructions", callback_data="admin_view_orders_instruct")
    )

    if command == 'dashboard':
        edit_message(
            text="⚙️ **Admin Dashboard**\n\nSelect an action:",
            reply_markup=get_admin_dashboard_keyboard()
        )
        return

    elif command == 'menu':
        menu = db_manager.get_menu()
        if menu:
            menu_text = "📋 **Current Menu Items**\n\n"
            for item in menu:
                # Use Markdown
                menu_text += f"**ID {item['id']}: {item['name'].title()}** - *₹{item['price']:.2f}*\n"
            edit_message(menu_text, back_to_dashboard)
        else:
            edit_message("📋 The menu is currently empty.", back_to_dashboard)
        return

    elif command == 'stats':
        stats = db_manager.get_order_statistics()
        if stats:
            # CRITICAL FIX: Using PLAIN TEXT to guarantee no Markdown parsing errors
            stats_text = (
                f"📈 Canteen Statistics\n\n"
                f"Total Orders: {stats['total_orders']}\n"
                f"Total Revenue: ₹{stats['total_revenue']:.2f}\n"
                f"Today's Orders: {stats['today_orders']}\n"
                f"Orders by Status:\n"
            )
            for status, count in stats['status_counts'].items():
                stats_text += f"- {status.replace('_', ' ').title()}: {count}\n"

            edit_message(stats_text, back_to_dashboard, parse_mode=None)  # Use PLAIN TEXT
        else:
            edit_message("📈 Unable to retrieve statistics.", back_to_dashboard)
        return

    # --- UNIFIED INSTRUCTIONS PANEL (Triggered by 'Instructions' button) ---
    elif command == 'help':
        help_text = (
            f"❓ **Admin Instructions & Commands**\n\n"
            f"All management functions are performed using **text commands**.\n"
            f"-----------------------------------------\n"

            f"🧾 **Order Viewing Commands**:\n"
            f"1. **Live Orders**: Type `/todayorders`\n"
            f"2. **Archived History**: Type `/viewarchive`\n"
            f"-----------------------------------------\n"

            f"📋 **Menu Management Commands**:\n"

            f"**1. Add New Item:**\n"
            f"• **Syntax**: `add <Item Name> <Price>`\n"
            f"• **Example**: `add Chicken Roll 80`\n\n"

            f"**2. Update Price:**\n"
            f"• **Syntax**: `update <Item ID> <New Price>`\n"
            f"• **Example**: `update 5 15.50`\n\n"

            f"**3. Delete/Remove Item:**\n"
            f"• **Syntax**: `delete <Item ID>`\n"
            f"• **Example**: `delete 3`\n\n"

            f"*Tip*: Use 'View Menu' to find the Item ID first."
        )
        edit_message(help_text, back_to_dashboard)
        return

    # --- ORDER VIEW INSTRUCTIONS (Triggered by 'View Orders' button) ---
    elif command == 'view_orders_instruct':
        instruct_text = (
            "🧾 **Order Viewing Instructions**\n\n"
            "To view orders, please use the following **text commands**:\n\n"
            "1. **View Today's Orders (Live):**\n"
            "   Type `/todayorders`\n"
            "   (Shows all orders from the current day)\n\n"
            "2. **View Archived Orders (History):**\n"
            "   Type `/viewarchive`\n"
            "   (Displays clickable archive files for previous days)\n"
        )
        edit_message(instruct_text, back_to_dashboard)
        return

    # This callback is used when hitting the 'Back to Archives' button after viewing a file
    elif command == 'view_archives' and command_type == 'admin':
        # Rebuild the archive list using the dedicated handler
        view_archives_command_handler(chat_id)
        return


    # --- DISPLAY SPECIFIC ARCHIVE FILE (Triggered by inline button from /viewarchive text command) ---
    elif command_type == 'archive_view_file':
        filename = data.split(':')[1]
        archived_orders = db_manager.get_archived_orders_by_filename(filename)

        if not archived_orders:
            archive_text = f"❌ **Archive Error**\n\nFile not found or corrupted: `{filename}`"
            edit_message(archive_text, back_to_orders_instruct)
            return

        date_part = filename.replace('orders_archived_before_', '').replace('.json', '')
        try:
            # FIX: Calculate the date the data was created (one day before cutoff date)
            cutoff_date = datetime.strptime(date_part, '%Y-%m-%d')
            data_date = cutoff_date - timedelta(days=1)
            display_date = data_date.strftime('%d %b %Y')
        except ValueError:
            display_date = "N/A"

        archive_text = f"📄 **Archived Orders (Data up to {display_date})** ({len(archived_orders)} total)\n\n"

        for order in archived_orders:
            status_emoji = {
                'pending': '🟡', 'payment_pending': '🟠', 'paid': '🟢',
                'cancelled': '🔴', 'expired': '⚫', 'delivered': '🔵'
            }.get(order.get('status', 'N/A'), '⚪')

            items_data = order.get('items', [])
            item_summary = ", ".join([f"{item.get('name', 'Item')} x{item.get('qty', 1)}" for item in items_data])

            order_id = order.get('id', 'N/A')
            status = order.get('status', 'N/A').title()
            total = order.get('total_amount', 0.0)

            archive_text += (
                f"{status_emoji} **Order #{order_id}** - {status} - ₹{total:.2f}\n"
                f"  - Items: {item_summary}\n"
            )

        # back_to_orders_instruct points to the order instructions panel.
        edit_message(archive_text, back_to_orders_instruct)
        return


# --- TELEGRAM BOT HANDLERS ---

@bot.message_handler(func=lambda message: True)
def handle_incoming_message(message: Message):
    """Processes all incoming Telegram messages (admin commands, typed quantity, contact share, etc.)."""
    try:
        incoming_msg = message.text.strip() if message.text else ''
        incoming_msg_lower = incoming_msg.lower()
        from_chat_id = message.chat.id
        student_db_id = str(from_chat_id)
        current_state = db_manager.get_session_state(student_db_id)

        print(f"📨 Message from {from_chat_id}: '{incoming_msg}' (State: {current_state})")

        # --- ADMIN COMMANDS ---
        if from_chat_id in ADMIN_CHAT_IDS:
            if incoming_msg == 'Admin Panel ⚙️':
                bot.send_message(
                    from_chat_id,
                    "⚙️ **Admin Dashboard**\n\nSelect an action:",
                    parse_mode='Markdown',
                    reply_markup=get_admin_dashboard_keyboard()
                )
                return

            # Handle all admin text commands (menu management AND new view commands)
            if incoming_msg_lower.startswith(('add ', 'update ', 'delete ')) or incoming_msg_lower in ['/todayorders',
                                                                                                       '/today',
                                                                                                       '/liveorders',
                                                                                                       '/viewarchive',
                                                                                                       '/archive',
                                                                                                       '/history']:
                handle_admin_text_commands(incoming_msg_lower, from_chat_id)
                return

        # --- NEW: HANDLE PHONE NUMBER INPUT (Plain Text) ---
        if current_state == 'awaiting_phone_number' and message.content_type == 'text':
            phone_number = message.text.strip()
            current_order_id = db_manager.get_session_order_id(student_db_id)

            # CRITICAL FIX: Check if the user is attempting to CANCEL
            if phone_number.lower() == 'cancel order ❌':
                db_manager.update_order_status(current_order_id, 'cancelled')
                db_manager.set_session_state(student_db_id, 'initial', None)
                bot.send_message(from_chat_id, "❌ Order cancelled. Tap 'Menu 🍽️' below to start a new order.",
                                 reply_markup=get_main_reply_keyboard())
                return

            # Basic validation: Check if it looks like a phone number (digits, plus sign, etc.)
            # Accepts 6 to 15 digits, optionally prefixed by +
            if not re.fullmatch(r'^\+?\d{6,15}$', phone_number.replace(' ', '').replace('-', '')):
                bot.send_message(
                    from_chat_id,
                    "❌ Invalid format. Please enter a valid phone number (6-15 digits), including the country code (e.g., `+919876543210`) or just the 10 digits.",
                    reply_markup=get_phone_entry_keyboard()  # Keep the phone entry keyboard visible
                )
                return

            # Validation Passed. Process number.
            # 1. Store phone number
            db_manager.update_user_phone(student_db_id, phone_number)

            # 2. Reset state and proceed to checkout confirmation
            db_manager.set_session_state(student_db_id, 'confirming_order', current_order_id)

            # 3. CRITICAL FIX: Send ReplyKeyboardRemove to clear the number entry keyboard
            bot.send_message(from_chat_id, "Contact saved. Resuming checkout...", reply_markup=ReplyKeyboardRemove())

            # 4. Trigger the final confirmation message logic here
            order = db_manager.get_order_details(current_order_id)

            if order:
                items_list = db_manager.parse_order_items(order['items'])
                food_summary = "\n".join([
                    f"• {item['name'].title()} x {item['qty']} (₹{item['price']:.2f})"
                    for item in items_list
                ])

                # Use saved phone for confirmation display
                contact_display = phone_number

                confirmation_msg = (
                    f"📝 *Final Order Confirmation (ID: #{order['id']}):*\n\n"
                    f"📞 **Contact:** `{contact_display}` (Saved)\n"
                    f"🪑 **Service Type:** {order.get('service_type', 'N/A').replace('_', ' ').title()}\n"
                    f"💰 **Total Amount:** ₹{order['total_amount']:.2f}\n\n"
                    f"🍽️ *Items:*\n{food_summary}\n\n"
                    f"Press **'✅ Confirm & Pay'** to proceed to Razorpay."
                )

                bot.send_message(
                    from_chat_id,
                    confirmation_msg,
                    parse_mode='Markdown',
                    reply_markup=get_confirmation_inline_keyboard()
                )
            else:
                bot.send_message(from_chat_id, "❌ Error: Could not retrieve order details. Please start over.",
                                 reply_markup=get_main_reply_keyboard())

            return

        # --- NEW: HANDLE CONTACT SHARING (Legacy Button, still needs to be supported) ---
        if message.content_type == 'contact' and current_state == 'awaiting_phone_number':
            phone_number = message.contact.phone_number
            current_order_id = db_manager.get_session_order_id(student_db_id)

            # 1. Store phone number
            db_manager.update_user_phone(student_db_id, phone_number)

            # 2. Reset state and proceed to checkout confirmation
            db_manager.set_session_state(student_db_id, 'confirming_order', current_order_id)

            # 3. CRITICAL FIX: Send ReplyKeyboardRemove to clear the contact button
            bot.send_message(from_chat_id, "Contact saved. Resuming checkout...", reply_markup=ReplyKeyboardRemove())

            # 4. Trigger the final confirmation message logic here
            order = db_manager.get_order_details(current_order_id)

            if order:
                items_list = db_manager.parse_order_items(order['items'])
                food_summary = "\n".join([
                    f"• {item['name'].title()} x {item['qty']} (₹{item['price']:.2f})"
                    for item in items_list
                ])

                # Use saved phone for confirmation display
                contact_display = phone_number

                confirmation_msg = (
                    f"📝 *Final Order Confirmation (ID: #{order['id']}):*\n\n"
                    f"📞 **Contact:** `{contact_display}` (Saved)\n"
                    f"🪑 **Service Type:** {order.get('service_type', 'N/A').replace('_', ' ').title()}\n"
                    f"💰 **Total Amount:** ₹{order['total_amount']:.2f}\n\n"
                    f"🍽️ *Items:*\n{food_summary}\n\n"
                    f"Press **'✅ Confirm & Pay'** to proceed to Razorpay."
                )

                bot.send_message(
                    from_chat_id,
                    confirmation_msg,
                    parse_mode='Markdown',
                    reply_markup=get_confirmation_inline_keyboard()
                )
            else:
                bot.send_message(from_chat_id, "❌ Error: Could not retrieve order details. Please start over.",
                                 reply_markup=get_main_reply_keyboard())

            return

        # --- HANDLE TYPED QUANTITY INPUT (Unchanged) ---
        if current_state.startswith('awaiting_typed_quantity_'):
            try:
                quantity = int(message.text.strip())
                item_id = int(current_state.split('_')[-1])

                if quantity <= 0 or quantity > 100:
                    bot.send_message(from_chat_id,
                                     "⚠️ Quantity must be a number between 1 and 100. Please try again or tap '↩️ Back to Menu'.",
                                     reply_markup=get_main_reply_keyboard())
                    return

                add_item_to_cart_and_prompt(student_db_id, from_chat_id, message.message_id, item_id, quantity)

            except ValueError:
                bot.send_message(from_chat_id,
                                 "❌ Invalid input. Please enter a whole number for the quantity or tap 'Menu 🍽️' to restart.",
                                 reply_markup=get_main_reply_keyboard())

            db_manager.set_session_state(student_db_id, 'awaiting_add_more',
                                         db_manager.get_session_order_id(student_db_id))
            return

        # --- UNIVERSAL COMMANDS (Unchanged) ---
        if incoming_msg_lower in ['menu', 'hi', 'hello', 'start', 'restart', '/start', 'menu 🍽️',
                                  'cancel/back to menu ❌']:

            if from_chat_id in ADMIN_CHAT_IDS:
                bot.send_message(from_chat_id, "💬 Welcome back! Select an option below.",
                                 reply_markup=get_admin_reply_keyboard())
                return

            # If state is awaiting_phone_number and they type menu/cancel, they go back to main menu
            if current_state == 'awaiting_phone_number':
                db_manager.set_session_state(student_db_id, 'initial', None)
                # Re-send the welcome message without the phone request
                bot.send_message(from_chat_id, "❌ Phone request cancelled. You can restart your order.",
                                 reply_markup=get_main_reply_keyboard())
                return

            # For regular users, start the menu flow
            start_menu_flow(student_db_id, from_chat_id)
            return

        elif incoming_msg_lower in ['status', 'order status', 'order status 📊']:
            handle_status_check(student_db_id, from_chat_id)
            return

        # --- FALLBACK (Unchanged) ---
        else:
            if from_chat_id in ADMIN_CHAT_IDS:
                reply_markup = get_admin_reply_keyboard()
                bot.send_message(from_chat_id,
                                 "💬 Welcome! Tap 'Admin Panel ⚙️' or 'Menu 🍽️' below.",
                                 reply_markup=reply_markup)
            else:
                reply_markup = get_main_reply_keyboard()
                bot.send_message(from_chat_id,
                                 "💬 I'm ready to take your order! Tap 'Menu 🍽️' to start.",
                                 reply_markup=reply_markup)


    except Exception as e:
        # The generic fallback message is sufficient
        print(f"❌ Error handling incoming message: {e}")
        traceback.print_exc()
        bot.send_message(message.chat.id,
                         "❌ Sorry, there was an error processing your request. Please try again or tap 'Menu 🍽️'.",
                         reply_markup=get_main_reply_keyboard())


@bot.callback_query_handler(func=lambda call: True)
def handle_inline_callbacks(call):
    """Handles all inline button clicks (item selection, quantity, checkout, admin commands, etc.)."""

    student_db_id = str(call.message.chat.id)
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data

    try:
        # --- CRITICAL FIX: Gracefully handle "query is too old" error ---
        try:
            bot.answer_callback_query(call.id)
        except telebot.apihelper.ApiTelegramException as e:
            if 'query is too old' in str(e):
                print(f"⚠️ Warning: Ignoring stale callback query from {chat_id}. Query ID: {call.id}")
                return
            else:
                raise

        # --- ADMIN CALLBACKS (Includes archive_view_file) ---
        if chat_id in ADMIN_CHAT_IDS and (data.startswith('admin_') or data.startswith('archive_view_file:')):
            handle_admin_callbacks(data, chat_id, message_id)
            return

        # --- NEW DELIVERY CALLBACK (Unchanged) ---
        if data.startswith('delivered:'):
            order_id = int(data.split(':')[1])

            # 1. Update database status
            db_manager.update_order_status(order_id, 'delivered')

            # 2. Rebuild the message content to show delivered status
            order_details = db_manager.get_order_details(order_id)

            if order_details:
                items_list = db_manager.parse_order_items(order_details['items'])
                food_summary = "\n".join([
                    f"• {item['name'].title()} x {item['qty']} (₹{item['price']:.2f})"
                    for item in items_list
                ])

                # Use the student_phone value directly
                student_identifier = order_details['student_phone']

                # Use standard Markdown for the final, delivered message for simpler display
                updated_text = (
                    f"🚨 **ORDER CONFIRMED & PAID!** 🚨\n\n"
                    f"🆔 **Order ID:** #{order_id}\n"
                    f"🔢 **Verification Code:** `{order_details.get('pickup_code', 'N/A')}`\n"
                    f"💰 **Total Amount:** ₹{order_details['total_amount']:.2f}\n"
                    f"📞 **Student Phone/ID:** `{student_identifier}`\n"
                    f"🪑 **Service Type:** *{order_details.get('service_type', 'N/A').title()}*\n\n"
                    f"🍽️ **Ordered Items:**\n{food_summary}\n\n"
                    f"**🔵 STATUS: DELIVERED** (Marked by Admin)"
                )
            else:
                updated_text = f"✅ Order #{order_id} marked as DELIVERED in the database. (Original message details lost upon edit)"

            try:
                # Edit the message to show the final delivered status and remove the button
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=updated_text,
                    parse_mode='Markdown',  # Use Markdown for the final display
                    reply_markup=None  # Remove the button
                )
            except telebot.apihelper.ApiTelegramException as e:
                print(f"⚠️ Failed to edit message for delivery confirmation: {e}")
                # Fallback: send a new message
                bot.send_message(chat_id, f"✅ Order #{order_id} marked as DELIVERED.")

            return

        # --- UNIVERSAL CALLBACKS (Unchanged) ---
        if data == 'menu_start':
            start_menu_flow(student_db_id, chat_id, message_id)
            return

        elif data == 'cancel_order':
            current_order_id = db_manager.get_session_order_id(student_db_id)
            if current_order_id:
                db_manager.update_order_status(current_order_id, 'cancelled')
            db_manager.set_session_state(student_db_id, 'initial', None)

            reply_markup = get_admin_reply_keyboard() if chat_id in ADMIN_CHAT_IDS else get_main_reply_keyboard()

            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="❌ Order cancelled. Tap 'Menu 🍽️' below to start a new order.",
                reply_markup=None  # Remove inline buttons
            )
            bot.send_message(chat_id, "Menu options are available below.", reply_markup=get_main_reply_keyboard())
            return

        elif data.startswith('copy_razorpay_'):
            order_id = data.split('_')[-1]
            order_details = db_manager.get_order_details(int(order_id))
            if order_details and order_details.get('payment_link'):
                copy_msg = (
                    f"📋 *Razorpay Payment Link for Order #{order_id}:*\n\n"
                    f"`{order_details['payment_link']}`\n\n"
                    f"💰 *Amount:* ₹{order_details['total_amount']:.2f}"
                )
                bot.send_message(chat_id, copy_msg, parse_mode='Markdown')
            return

        # --- ORDERING FLOW CALLBACKS ---
        current_order_id = db_manager.get_session_order_id(student_db_id)

        # 1. ITEM SELECTION: data='item:<item_id>'
        if data.startswith('item:'):
            item_id = int(data.split(':')[1])
            item = db_manager.get_menu_item(item_id)

            if not item or current_order_id is None:
                start_menu_flow(student_db_id, chat_id, message_id,
                                error_msg="⚠️ Error processing item/order. Restarting.")
                return

            # Edit message to show quantity buttons
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"📦 You selected *{item['name'].title()}* (₹{item['price']:.2f}).\n\n"
                     f"Please select the **quantity** required for this item:",
                parse_mode='Markdown',
                reply_markup=get_quantity_inline_keyboard(item_id)
            )
            return

        # 2. QUANTITY SELECTION (BUTTON): data='qty:<item_id>:<quantity>'
        elif data.startswith('qty:'):
            _, item_id_str, quantity_str = data.split(':')
            item_id = int(item_id_str)
            quantity = int(quantity_str)

            # This is an inline callback, so message_id is the bot's message ID (EDITABLE)
            add_item_to_cart_and_prompt(student_db_id, chat_id, message_id, item_id, quantity)
            return

        # 2. QUANTITY SELECTION (TYPE INPUT TRIGGER): data='type_qty:<item_id>'
        elif data.startswith('type_qty:'):
            item_id = int(data.split(':')[1])
            item = db_manager.get_menu_item(item_id)

            # CRITICAL: Change state to awaiting_typed_quantity
            db_manager.set_session_state(student_db_id, f'awaiting_typed_quantity_{item_id}', current_order_id)

            # Edit the message to show the prompt for typing
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✍️ Please **type the quantity** you require for *{item['name'].title()}* (e.g., `8`).",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ Back to Menu", callback_data="menu_start")]
                ])
            )
            return


        # 3. ADD MORE / CHECKOUT SELECTION: data='add_more' or 'checkout'
        elif data == 'add_more':
            # Go back to menu selection
            start_menu_flow(student_db_id, chat_id, message_id)
            return

        elif data == 'checkout':
            # Proceed to service type selection
            current_order_id = db_manager.get_session_order_id(student_db_id)
            if not current_order_id or not db_manager.get_order_details(current_order_id):
                 # Fail gracefully if order is somehow lost
                start_menu_flow(student_db_id, chat_id, message_id, error_msg="⚠️ Order details lost. Restarting.")
                return

            db_manager.set_session_state(student_db_id, 'awaiting_service_type', current_order_id)
            
            # Show checkout message
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="🍴 **Checkout:** How would you like your order?",
                parse_mode='Markdown',
                reply_markup=get_service_type_inline_keyboard()
            )
            return

        # 4. SERVICE TYPE SELECTION: data='service:<type>'
        elif data.startswith('service:'):
            service_type = data.split(':')[1]
            current_order_id = db_manager.get_session_order_id(student_db_id)

            # CRITICAL SAFETY CHECK
            if not current_order_id or not db_manager.get_order_details(current_order_id):
                start_menu_flow(student_db_id, chat_id, message_id, error_msg="⚠️ Order error. Restarting.")
                return
            
            # --- Continue processing ---
            db_manager.update_order_service_type(current_order_id, service_type)
            order = db_manager.get_order_details(current_order_id)

            # CRITICAL CHECK: Ask for phone number here if needed
            if not db_manager.get_user_phone(student_db_id):
                # Save current state and order ID
                db_manager.set_session_state(student_db_id, 'awaiting_phone_number', current_order_id)

                # Send prompt for contact info
                prompt_for_phone_number(student_db_id, chat_id)
                # Edit the previous bot message to remove the inline buttons
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="⌛️ Waiting for contact number...",
                    reply_markup=None
                )
                return

            items_list = db_manager.parse_order_items(order['items'])
            food_summary = "\n".join([
                    f"• {item['name'].title()} x {item['qty']} (₹{item['price']:.2f})"
                    for item in items_list
                ])

            # Use saved phone for confirmation display
            contact_display = db_manager.get_user_phone(student_db_id)

            confirmation_msg = (
                f"📝 *Final Order Confirmation (ID: #{current_order_id}):*\n\n"
                f"📞 **Contact:** `{contact_display}`\n"
                f"🪑 **Service Type:** {service_type.replace('_', ' ').title()}\n"
                f"💰 **Total Amount:** ₹{order['total_amount']:.2f}\n\n"
                f"🍽️ *Items:*\n{food_summary}\n\n"
                f"Press **'✅ Confirm & Pay'** to proceed to Razorpay."
            )

            db_manager.set_session_state(student_db_id, 'confirming_order', current_order_id)
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=confirmation_msg,
                parse_mode='Markdown',
                reply_markup=get_confirmation_inline_keyboard()
            )
            return

        # 5. CONFIRMATION/PAYMENT: data='confirm_pay'
        elif data == 'confirm_pay':
            order = db_manager.get_order_details(current_order_id)
            if not order:
                start_menu_flow(student_db_id, chat_id, message_id, error_msg="❌ Order not found. Restarting.")
                return

            # RE-FIX: If for some reason the state was skipped, block payment until number is present
            if not db_manager.get_user_phone(student_db_id):
                db_manager.set_session_state(student_db_id, 'awaiting_phone_number', current_order_id)
                prompt_for_phone_number(student_db_id, chat_id)
                return

            total_amount = order['total_amount']
            
            # --- START FIX 1: CHECK FOR EXISTING PAYMENT LINK ---
            razorpay_order_id = order.get('razorpay_order_id')
            payment_link = order.get('payment_link')
            
            if razorpay_order_id and payment_link:
                # Use existing link to avoid Razorpay error
                print(f"💰 Using existing Razorpay Payment Link for Order #{current_order_id}")
            else:
                # Generate new link only if none exists
                try:
                    razorpay_order_id, payment_link = generate_razorpay_payment_link(current_order_id, total_amount, student_db_id)
                except requests.exceptions.ConnectionError:
                    print("❌ Network connection failed during Razorpay link generation. Resetting session.")
                    db_manager.set_session_state(student_db_id, 'initial', None)
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="❌ Connection failed while talking to Razorpay. Please tap 'Menu 🍽️' to try a new order.",
                        reply_markup=get_main_reply_keyboard()
                    )
                    return
                # FIX PAYMENT ERROR: Catching the specific Razorpay Bad Request error here
                except razorpay.errors.BadRequestError as e:
                    if "reference_id" in str(e):
                        print(f"❌ Razorpay Conflict Error: {e}. Forcing session reset.")
                        db_manager.set_session_state(student_db_id, 'initial', None)
                        bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text="❌ Payment link error (Order ID conflict). Please tap 'Menu 🍽️' to start a *new* order.",
                            reply_markup=get_main_reply_keyboard()
                        )
                        return
                    else:
                        raise # Re-raise other unknown BadRequestErrors
            # --- END FIX 1 ---

            if razorpay_order_id and payment_link:
                # Only update DB if a *new* link was generated or if details were missing
                if not order.get('razorpay_order_id'):
                    db_manager.update_razorpay_details(current_order_id, razorpay_order_id, payment_link)
                    
                db_manager.update_order_status(current_order_id, 'payment_pending')

                payment_keyboard = create_payment_keyboard(payment_link, current_order_id)
                
                # --- START FIX 2: Check if file exists before using open() (though usually fine on Render) ---
                payment_qr_path = generate_payment_qr_code(payment_link, current_order_id)
                # --- END FIX 2 ---


                payment_msg = (
                    f"✅ *Order Ready for Payment! (ID: #{current_order_id})*\n\n"
                    f"💰 **Total Amount:** ₹{total_amount:.2f}\n\n"
                    f"💳 **Pay Securely with Razorpay:**\n"
                    f"👆 Tap the button or scan the QR code below.\n"
                    f"Status updates automatically after payment."
                )

                # Edit the confirmation message to display the payment QR/Link
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="⏳ Generating payment link and QR code...",
                    reply_markup=None  # Remove old buttons first
                )

                if payment_qr_path:
                    # FIX: Sending the photo with PLAIN TEXT parse_mode=None to avoid Markdown crash (Error 400 fix)
                    with open(payment_qr_path, 'rb') as photo:
                        bot.send_photo(chat_id, photo, caption=payment_msg, parse_mode='Markdown',
                                       reply_markup=payment_keyboard)
                else:
                    bot.send_message(chat_id, payment_msg, parse_mode='Markdown', reply_markup=payment_keyboard)

                db_manager.set_session_state(student_db_id, 'waiting_for_payment', current_order_id)
                return
            else:
                # This catches Razorpay internal errors or invalid response
                db_manager.set_session_state(student_db_id, 'initial', None)
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="❌ Could not generate payment link. Please try again or contact support.",
                    reply_markup=None
                )
                bot.send_message(chat_id, "Tap 'Menu 🍽️' to try again.", reply_markup=get_main_reply_keyboard())
                return


    except Exception as e:
        # We catch all other errors here and handle them as a fallback.
        print(f"❌ Error handling callback query: {e}")
        traceback.print_exc()
        # Fallback: Send a message to restart the flow
        try:
            error_message = "❌ An internal error occurred! Please tap 'Menu 🍽️' to restart the flow."
            bot.send_message(chat_id, error_message, reply_markup=get_main_reply_keyboard())
        except Exception:
            # If sending the message fails, the connection is totally dead.
            pass


def prompt_for_phone_number(student_db_id, chat_id):
    """Prompts the user to share their phone number before final confirmation."""

    db_manager.set_session_state(student_db_id, 'awaiting_phone_number', db_manager.get_session_order_id(student_db_id))

    msg = (
        "📞 **We need your contact info!**\n\n"
        "Please **type your mobile number** (e.g., `+919876543210`) or just the 10 digits to finalize payment.\n\n"
        "*(If you wish to cancel the order, use the button below.)*"
    )

    # We send this message, and rely on the CONTACT handler to resume the process.
    bot.send_message(
        chat_id,
        msg,
        parse_mode='Markdown',
        reply_markup=get_phone_entry_keyboard()
    )


def start_menu_flow(student_db_id, chat_id, message_id=None, error_msg=None):
    """
    Initiates the menu flow using Inline Keyboards.
    """

    # 1. Check for active order or create a new one
    current_order_id = db_manager.get_session_order_id(student_db_id)

    if current_order_id is not None:
        order_details = db_manager.get_order_details(current_order_id)
        if order_details and order_details.get('status', 'cancelled') != 'pending':
            current_order_id = None

    if current_order_id is None:
        new_order_id = db_manager.create_order(student_db_id, [], 0.0, 'pending')
        if new_order_id is None:
            bot.send_message(chat_id, "❌ Critical Error: Could not start a new order. Please try again.",
                             reply_markup=get_main_reply_keyboard())
            return

        current_order_id = new_order_id

    # 2. Update session state
    db_manager.set_session_state(student_db_id, 'ordering_item', current_order_id)

    menu = db_manager.get_menu()

    main_message = f"🍽️ *Welcome to Digital Canteen!* 👋\n\n"
    if error_msg:
        main_message = f"{error_msg}\n\n" + main_message

    if menu:
        main_message += "📋 *Please select an item to order:*"

        if message_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=main_message,
                parse_mode='Markdown',
                reply_markup=get_menu_inline_keyboard(student_db_id)
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text=main_message,
                parse_mode='Markdown',
                reply_markup=get_menu_inline_keyboard(student_db_id)
            )

    else:
        bot.send_message(
            chat_id,
            "😔 Sorry, the menu is currently empty. Please check back later or contact the canteen staff.",
            reply_markup=get_main_reply_keyboard()
        )


def handle_status_check(student_db_id, chat_id):
    """
    Helper function to handle the Order Status check.
    FIX: Ensure status_msg uses Plain Text to avoid crashing on Markdown errors.
    """

    # Safely get the order ID
    current_order_id_obj = db_manager.get_session_order_id(student_db_id)

    if not current_order_id_obj:
        bot.send_message(chat_id, "❌ No active order found. Tap 'Menu 🍽️' to place a new order.",
                         reply_markup=get_main_reply_keyboard())
        return

    current_order_id = int(current_order_id_obj)
    order_details = db_manager.get_order_details(current_order_id)

    # CRITICAL FIX: Ensure order_details is not None and status is not cancelled before proceeding
    if order_details and order_details.get('status') and order_details['status'] != 'cancelled':
        # Safely parse order items (handle case where items might be empty/None)
        items_list = db_manager.parse_order_items(order_details.get('items', '[]'))

        # Build food summary for PLAIN TEXT display
        food_summary = "\n".join([
            f"- {item['name'].title()} x {item['qty']} (₹{item['price']:.2f})"
            for item in items_list
        ])
        service_type = order_details.get('service_type', 'N/A').title()

        # Switch to PLAIN TEXT for robust display (parse_mode=None)
        status_msg = (
            f"📊 Order Status (ID: #{current_order_id})\n\n"
            f"📋 Current Status: {order_details['status'].title()}\n"
            f"💰 Total Amount: ₹{order_details['total_amount']:.2f}\n"
            f"🪑 Service Type: {service_type}\n"
            f"🔢 Pickup Code: {order_details.get('pickup_code', 'N/A')}\n\n"
            f"🍽️ Items:\n{food_summary}"
        )

        # Send as Plain Text (parse_mode=None)
        bot.send_message(chat_id, status_msg, parse_mode=None, reply_markup=get_main_reply_keyboard())
    else:
        bot.send_message(chat_id,
                         "❌ No active order found or your last order was cancelled. Tap 'Menu 🍽️' to place a new order.",
                         reply_markup=get_main_reply_keyboard())


def start_cleanup_thread():
    """
    Start a background thread to periodically clean up expired sessions
    and run the daily archive/reset check.
    """

    def cleanup_worker():
        while True:
            try:
                # 1. Run the daily archive and reset logic
                db_manager.archive_and_reset_daily_orders()

                # 2. Cleanup old sessions (e.g., sessions older than 7 days)
                db_manager.cleanup_old_sessions(days_old=7)

                # Check every 5 minutes (300 seconds)
                time.sleep(300)
            except Exception as e:
                print(f"❌ Error in cleanup thread: {e}")
                time.sleep(60)

    cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
    cleanup_thread.start()
    print("🧹 Started background cleanup thread (runs every 5 minutes, checking for daily reset)")


# --- TELEGRAM BOT POLLING FUNCTION (RESTORED) ---
def run_bot():
    """Starts the Telegram bot polling loop."""
    print("\n🚀 Starting Telegram Bot Polling...")
    print("   📡 Bot is now listening for messages...")
    print("   ⏹️  Press Ctrl+C to stop\n")
    print("=" * 50)

    # CRITICAL FIX: Clear any active webhook before starting polling
    try:
        # NOTE: Even if webhook is already deleted, the 409 conflict can still occur if two threads try to poll simultaneously.
        if bot.delete_webhook():
            print("✅ Successfully cleared existing Telegram webhook.")
    except Exception as e:
        print(f"⚠️ Warning: Could not delete webhook on startup: {e}")

    # Check for Gunicorn environment variable (GUNICORN_PID is set in Gunicorn/Web service mode)
    if 'GUNICORN_PID' in os.environ:
        print("⚠️ Gunicorn detected. Skipping run_bot() to prevent Code 409 conflict.")
        return

    while True:
        try:
            # Note: Changed from none_stop to non_stop to address deprecation warning
            bot.polling(non_stop=True, interval=3)
        except Exception as e:
            print(f"❌ Polling failed due to fatal error: {e}. Retrying in 5 seconds...")
            time.sleep(5)
        except KeyboardInterrupt:
            break

# --- FLASK SERVER & BOT STARTUP (FIXED) ---

# We remove the run_flask function and the manual threading to avoid conflicts.
# The Gunicorn command in the Web Service handles the web server part.
# The 'python app.py' command in the Polling Service handles the bot and threads.

# CRITICAL FIX: Call setup_flask_routes here so that Gunicorn always loads all routes.
setup_flask_routes()

# --- NEW: RUN FLASK SERVER IN A THREAD (FOR SINGLE SERVICE MODE) ---
def run_flask():
    """Starts the Flask server in a separate thread, bound to the PORT environment variable."""
    # Render requires the app to bind to the port defined by its environment.
    # We use 5001 as a fallback for local testing, but it will use the Render-assigned port.
    PORT = int(os.environ.get("PORT", 5001))
    print(f"🌐 Starting Flask server in background thread on port {PORT}...")
    # NOTE: We must disable Flask's reloader when running in a thread/production environment
    try:
        app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
    except Exception as e:
        print(f"❌ Critical Error: Could not start Flask thread on port {PORT}: {e}")
        # The main thread (polling) will continue to run even if the Flask thread fails to bind.

if __name__ == '__main__':
    if not os.getenv('BOT_TOKEN') or not RAZORPAY_CLIENT:
        print("\n🛑 Application setup incomplete. Check .env file.")
        exit(1)

    # Check if we are being run by Gunicorn (Web Service) or directly (Polling Service)
    # Gunicorn sets the GUNICORN_PID environment variable.
    # If GUNICORN_PID is set, we are running as the Web Service.
    if 'GUNICORN_PID' in os.environ:
        print("🌐 Running in Web Service Mode (Gunicorn). Skipping Polling/Threads setup.")
        # All routes are already set up above. No further startup is needed for the Web Service.
    
    # If GUNICORN_PID is NOT set, we are running as the Hybrid/Polling Service (python app.py)
    else:
        print("\n🔧 Initializing Telegram Canteen Bot (Hybrid Service Mode)...")
        print("=" * 50)
        
        # --- AGGRESSIVE DB RESET ---
        # NOTE: This call forces a fresh start and resets the order ID counter.
        if db_manager.aggressive_db_reset():
            print("✅ Database file reset successful.")
        
        print("🗃️  Setting up database...")
        if db_manager.create_tables():
            print("✅ Database tables created/verified successfully!")
            db_manager.add_default_menu_items()

            print("\n⏳ Running startup check for daily order archive and reset...")
            db_manager.archive_and_reset_daily_orders()
            print("-" * 50)

        else:
            print("❌ Database initialization failed!")
            exit(1)
        
        # --- START FLASK THREAD ---
        try:
            flask_thread = threading.Thread(target=run_flask, daemon=True)
            flask_thread.start()
            time.sleep(1) # Give the server a moment to bind to the port
        except Exception as e:
            print(f"❌ Critical Error: Could not start Flask thread: {e}")
            # Do not exit, as polling might still work, but webhooks will fail.

        start_cleanup_thread()
        db_manager.cleanup_old_sessions() # Aggressively clean up sessions before starting bot

        print("\n🔧 Bot Configuration:")
        print(f"   👤 Payee Name: {PAYEE_NAME}")
        print(f"   👨‍💼 Admin Chat IDs: {ADMIN_CHAT_IDS}")
        print(f"   🌐 Webhook URL: {BOT_PUBLIC_URL}/razorpay/webhook")
        print(f"   💾 Database: {db_manager.DATABASE_PATH}")

        print("=" * 50)

        # The run_bot function contains the actual polling loop
        try:
            run_bot()
        except KeyboardInterrupt:
            print("\n🛑 Bot stopped by user.")
        except Exception as e:
            print(f"❌ Error starting application: {e}")
            traceback.print_exc()

# The Flask application object 'app' must be at the top level for Gunicorn to find it.
# We do not need a final else/catch block outside of the polling run.
