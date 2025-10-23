# db_manager.py

import json
from pathlib import Path
from datetime import datetime, timedelta
import os
import logging
import psycopg2
import psycopg2.extras # For dictionary cursors
from supabase import create_client, Client
from dotenv import load_dotenv

# --- NEW CONFIGURATION ---
# Load environment variables from .env file (primarily for local testing)
BASE_DIR = Path(__file__).resolve().parent
DOTENV_PATH = BASE_DIR / '.env'
load_dotenv(dotenv_path=DOTENV_PATH)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- NEW SUPABASE CLIENT SETUP ---
SUPABASE_URL = os.getenv('SUPABASE_URL')
# Use the SERVICE KEY for full DB access from your backend
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY')
SUPABASE_DB_URL = os.getenv('SUPABASE_DB_URL')

# Basic check for essential variables
if not SUPABASE_URL or not SUPABASE_KEY or not SUPABASE_DB_URL:
    logging.error("❌ CRITICAL: Supabase environment variables (URL, KEY, DB_URL) are missing.")
    supabase = None # Ensure supabase client is None if setup fails
else:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("✅ Supabase client initialized (for storage).")
    except Exception as e:
        logging.error(f"❌ Error initializing Supabase client: {e}")
        supabase = None


# --- Database Connection ---

def create_connection():
    """
    Create a PostgreSQL database connection using the Supabase DB URL.
    Returns the connection object or None if connection fails.
    """
    if not SUPABASE_DB_URL:
        logging.error("❌ SUPABASE_DB_URL environment variable is not set.")
        return None
    try:
        conn = psycopg2.connect(SUPABASE_DB_URL)
        return conn
    except psycopg2.OperationalError as e:
        logging.error(f"❌ Database connection error: {e}")
        # Log specifics if possible (e.g., password auth failed)
        if "password authentication failed" in str(e):
            logging.error("   Hint: Check if SUPABASE_DB_URL includes the correct password.")
        return None
    except Exception as e:
        logging.error(f"❌ Unexpected error connecting to database: {e}")
        return None

# ========== USER SESSION OPERATIONS ==========

def set_session_state(user_id, state, order_id=None):
    """
    Sets the session state and current order ID for a user using UPSERT.
    """
    user_id_str = str(user_id) # Ensure user_id is a string for the TEXT PRIMARY KEY

    # UPSERT: Insert or Update in one command
    sql = """
        INSERT INTO users (id, session_state, current_order_id, last_active)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (id)
        DO UPDATE SET
            session_state = EXCLUDED.session_state,
            current_order_id = EXCLUDED.current_order_id,
            last_active = NOW();
    """
    conn = create_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id_str, state, order_id))
            conn.commit()
        logging.info(f"Session state for user {user_id_str} set to '{state}'.")
        return True
    except Exception as e:
        logging.error(f"Error setting session state for {user_id_str}: {e}")
        conn.rollback() # Rollback transaction on error
        return False
    finally:
        if conn:
            conn.close()

def get_session_state(user_id):
    """Retrieves the current session state for a user."""
    sql = "SELECT session_state FROM users WHERE id = %s;"
    conn = create_connection()
    if not conn:
        return 'initial' # Default state if DB connection fails
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(user_id),))
            state = cursor.fetchone()
        return state[0] if state and state[0] else 'initial'
    except Exception as e:
        logging.error(f"Error getting session state for {user_id}: {e}")
        return 'initial'
    finally:
        if conn:
            conn.close()

def get_session_order_id(user_id):
    """Retrieves the current order ID associated with a user's session."""
    sql = "SELECT current_order_id FROM users WHERE id = %s;"
    conn = create_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(user_id),))
            order_id = cursor.fetchone()
        return order_id[0] if order_id and order_id[0] else None
    except Exception as e:
        logging.error(f"Error getting session order ID for {user_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_user_phone(user_id):
    """Retrieves the stored phone number for a user by their chat ID."""
    sql = "SELECT phone_number FROM users WHERE id = %s;"
    conn = create_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(user_id),))
            phone = cursor.fetchone()
        return phone[0] if phone and phone[0] else None
    except Exception as e:
        # Don't log error if user simply doesn't exist yet
        if "relation \"users\" does not exist" not in str(e):
             logging.warning(f"Could not get user phone for {user_id} (may not exist yet): {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_user_phone(user_id, phone_number):
    """Updates the phone number for a user or creates a new user if not found."""
    sql = """
        INSERT INTO users (id, phone_number, last_active)
        VALUES (%s, %s, NOW())
        ON CONFLICT (id)
        DO UPDATE SET
            phone_number = EXCLUDED.phone_number,
            last_active = NOW();
    """
    conn = create_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(user_id), phone_number))
            conn.commit()
        logging.info(f"Phone number updated for user {user_id}.")
        return True
    except Exception as e:
        logging.error(f"Error updating phone number for {user_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

# ========== MENU OPERATIONS ==========

def get_menu():
    """Get all available menu items (including section)."""
    sql = "SELECT * FROM menu WHERE available = TRUE ORDER BY section, name;"
    conn = create_connection()
    if not conn:
        return []
    try:
        # Use DictCursor to get results as dictionaries
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute(sql)
            items = [dict(row) for row in cursor.fetchall()]
        return items
    except Exception as e:
        logging.error(f"Error getting menu: {e}")
        return []
    finally:
        if conn:
            conn.close()


def get_menu_item(item_id):
    """Get single menu item by ID (including section)."""
    sql = "SELECT * FROM menu WHERE id = %s AND available = TRUE;"
    conn = create_connection()
    if not conn:
        return None
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute(sql, (item_id,))
            item = cursor.fetchone()
        return dict(item) if item else None
    except Exception as e:
        logging.error(f"Error getting menu item {item_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def add_menu_item(name, price, section):
    """Add new menu item, or update price/section/availability if item already exists by name (case-insensitive)."""
    # Use ON CONFLICT with a lower-case index for case-insensitivity
    sql = """
        INSERT INTO menu (name, price, section, available)
        VALUES (%s, %s, %s, TRUE)
        ON CONFLICT (lower(name))
        DO UPDATE SET
            price = EXCLUDED.price,
            section = EXCLUDED.section,
            available = TRUE
        RETURNING id;
    """
    conn = create_connection()
    if not conn:
        return "❌ Database connection error"
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (name, price, section))
            result = cursor.fetchone()
            item_id = result[0] if result else None
            conn.commit()

        if item_id:
            # Cannot easily tell if it was insert or update without another query, keep it simple
            return f"✅ Synced '{name}' in *{section.title()}* section for ₹{price:.2f} (ID: {item_id})"
        else:
            # This case might happen with complex race conditions, though unlikely here
             return f"⚠️ Could not sync '{name}'. Please check logs."

    except Exception as e:
        logging.error(f"Error adding/updating menu item '{name}': {e}")
        conn.rollback()
        return f"❌ Error adding/updating '{name}'"
    finally:
        if conn:
            conn.close()

def update_menu_item(item_id, price):
    """Update menu item price by ID."""
    sql = "UPDATE menu SET price = %s WHERE id = %s RETURNING name;"
    conn = create_connection()
    if not conn:
        return "❌ Database connection error"
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (price, item_id))
            item = cursor.fetchone()
            conn.commit()

        if not item:
            return f"❌ Item ID {item_id} not found"

        return f"✅ Updated '{item[0]}' price to ₹{price:.2f}"
    except Exception as e:
        logging.error(f"Error updating menu item ID {item_id}: {e}")
        conn.rollback()
        return f"❌ Error updating item ID {item_id}"
    finally:
        if conn:
            conn.close()


def delete_menu_item(item_id):
    """Delete menu item (set as unavailable)."""
    sql = "UPDATE menu SET available = FALSE WHERE id = %s RETURNING name;"
    conn = create_connection()
    if not conn:
        return "❌ Database connection error"
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (item_id,))
            item = cursor.fetchone()
            conn.commit()

        if not item:
            return f"❌ Item ID {item_id} not found"

        return f"✅ Removed '{item[0]}' from menu (ID: {item_id})"
    except Exception as e:
        logging.error(f"Error deleting menu item ID {item_id}: {e}")
        conn.rollback()
        return f"❌ Error removing item ID {item_id}"
    finally:
        if conn:
            conn.close()

# ========== ORDER OPERATIONS ==========

def create_order(student_phone, order_details, total_amount, status='pending'):
    """Create a new order. Returns the new order ID."""
    # Ensure items are stored as a valid JSON string or psycopg2 can handle the dict/list
    items_json = json.dumps(order_details)
    sql = """
        INSERT INTO orders (student_phone, items, total_amount, status, created_at, updated_at)
        VALUES (%s, %s, %s, %s, NOW(), NOW())
        RETURNING id;
    """
    conn = create_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(student_phone), items_json, total_amount, status))
            order_id = cursor.fetchone()[0]
            conn.commit()
        logging.info(f"Order {order_id} created for user {student_phone}")
        return order_id
    except Exception as e:
        logging.error(f"Error creating order for {student_phone}: {e}")
        conn.rollback()
        return None
    finally:
        if conn:
            conn.close()

def parse_order_items(items_data):
    """
    Parses the items data (either JSON string or already parsed list/dict)
    into a Python list of dictionaries. Returns empty list on failure.
    """
    if not items_data:
        return []
    if isinstance(items_data, (list, dict)):
        return items_data # Already parsed (e.g., from JSONB)
    if isinstance(items_data, str):
        try:
            return json.loads(items_data)
        except json.JSONDecodeError as e:
            logging.error(f"Error decoding order items JSON string: {e}")
            return []
    logging.warning(f"Unexpected type for order items data: {type(items_data)}")
    return []


def get_order_details(order_id):
    """Get order details by ID."""
    sql = "SELECT * FROM orders WHERE id = %s;"
    conn = create_connection()
    if not conn:
        return None
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute(sql, (order_id,))
            order = cursor.fetchone()
        return dict(order) if order else None
    except Exception as e:
        logging.error(f"Error getting order details for {order_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()


def update_order_status(order_id, status):
    """Update order status."""
    sql = "UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s;"
    conn = create_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (status, order_id))
            success = cursor.rowcount > 0
            conn.commit()
        if success:
            logging.info(f"Order {order_id} status updated to '{status}'")
        else:
            logging.warning(f"Order {order_id} not found for status update to '{status}'")
        return success
    except Exception as e:
        logging.error(f"Error updating order {order_id} status to '{status}': {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def update_razorpay_details(order_id, razorpay_id, payment_link):
    """Updates the order with the Razorpay Payment Link ID and URL."""
    sql = """
        UPDATE orders
        SET razorpay_order_id = %s,
            payment_link = %s,
            updated_at = NOW()
        WHERE id = %s;
    """
    conn = create_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (razorpay_id, payment_link, order_id))
            success = cursor.rowcount > 0
            conn.commit()
        if success:
            logging.info(f"Razorpay details updated for order {order_id}")
        else:
            logging.warning(f"Order {order_id} not found for Razorpay update")
        return success
    except Exception as e:
        logging.error(f"Error updating Razorpay details for order {order_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def update_order_cart(order_id, current_items, new_total):
    """Updates the items list (as JSONB) and total amount for an ongoing order."""
    # psycopg2 can handle Python dict/list directly for JSONB
    sql = """
        UPDATE orders
        SET items = %s,
            total_amount = %s,
            updated_at = NOW()
        WHERE id = %s;
    """
    conn = create_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cursor:
             # Pass the Python list/dict directly
            cursor.execute(sql, (json.dumps(current_items), new_total, order_id))
            success = cursor.rowcount > 0
            conn.commit()
        if success:
            logging.info(f"Order cart updated for order {order_id}. New Total: ₹{new_total:.2f}")
        else:
             logging.warning(f"Order {order_id} not found for cart update.")
        return success
    except Exception as e:
        logging.error(f"Error updating order cart for {order_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def update_order_service_type(order_id, service_type):
    """Updates the service type for an ongoing order."""
    sql = "UPDATE orders SET service_type = %s, updated_at = NOW() WHERE id = %s;"
    conn = create_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (service_type, order_id))
            success = cursor.rowcount > 0
            conn.commit()
        if success:
            logging.info(f"Service type updated to '{service_type}' for order {order_id}")
        else:
             logging.warning(f"Order {order_id} not found for service type update.")
        return success
    except Exception as e:
        logging.error(f"Error updating service type for order {order_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def update_order_pickup_code(order_id, pickup_code):
    """Update pickup code for an order."""
    sql = "UPDATE orders SET pickup_code = %s, updated_at = NOW() WHERE id = %s;"
    conn = create_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (pickup_code, order_id))
            success = cursor.rowcount > 0
            conn.commit()
        if success:
            logging.info(f"Pickup code updated for order {order_id}")
        else:
            logging.warning(f"Order {order_id} not found for pickup code update")
        return success
    except Exception as e:
        logging.error(f"Error updating pickup code for order {order_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

def get_today_orders():
    """
    Retrieves all orders created today (based on the database server's time, usually UTC).
    """
    # Use ::date to cast the timestamp to just the date for comparison
    sql = "SELECT * FROM orders WHERE created_at >= NOW()::date ORDER BY created_at ASC;"
    conn = create_connection()
    if not conn:
        return []
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute(sql)
            orders = [dict(row) for row in cursor.fetchall()]
        logging.info(f"Fetched {len(orders)} orders for today.")
        return orders
    except Exception as e:
        logging.error(f"Error fetching today's orders: {e}")
        return []
    finally:
        if conn:
            conn.close()

def get_order_statistics():
    """Calculates and returns key statistics using PostgreSQL."""
    stats = {
        'total_orders': 0,
        'total_revenue': 0.0,
        'today_orders': 0,
        'status_counts': {}
    }
    conn = create_connection()
    if not conn:
        return stats # Return default stats on connection error
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            # 1. Total successful orders and revenue
            cursor.execute("""
                SELECT COUNT(id), COALESCE(SUM(total_amount), 0)
                FROM orders
                WHERE status IN ('paid', 'delivered');
            """)
            result = cursor.fetchone()
            stats['total_orders'] = result[0]
            stats['total_revenue'] = float(result[1]) # Ensure it's a float

            # 2. Today's successful orders
            cursor.execute("""
                SELECT COUNT(id)
                FROM orders
                WHERE created_at >= NOW()::date
                AND status IN ('paid', 'delivered');
            """)
            stats['today_orders'] = cursor.fetchone()[0]

            # 3. Counts for all statuses
            cursor.execute("SELECT status, COUNT(id) FROM orders GROUP BY status;")
            stats['status_counts'] = {row['status']: row['count'] for row in cursor.fetchall()}

        logging.info("Fetched order statistics.")
        return stats

    except Exception as e:
        logging.error(f"Error getting order statistics: {e}")
        return stats # Return default/partial stats on error
    finally:
        if conn:
            conn.close()

# --- Archive/Cleanup Functions Removed ---
# The pg_cron job scheduled via SQL handles daily cleanup.
