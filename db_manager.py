# db_manager.py

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
import time
import os
import logging
import psycopg2
import psycopg2.extras # For dictionary cursors
from supabase import create_client, Client
from dotenv import load_dotenv

# --- NEW CONFIGURATION ---
# Load environment variables from .env file
BASE_DIR = Path(__file__).resolve().parent
DOTENV_PATH = BASE_DIR / '.env'
load_dotenv(dotenv_path=DOTENV_PATH)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- NEW SUPABASE CLIENT SETUP ---
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY') # Use the SERVICE KEY for full DB access
SUPABASE_DB_URL = os.getenv('SUPABASE_DB_URL')

if not SUPABASE_URL or not SUPABASE_KEY or not SUPABASE_DB_URL:
    logging.error("❌ CRITICAL: Supabase environment variables (URL, KEY, DB_URL) are missing.")
    # In a serverless environment, we might not want to exit(1)
    # but we must ensure the client is not None
    supabase = None
else:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("✅ Supabase client initialized.")
    except Exception as e:
        logging.error(f"❌ Error initializing Supabase client: {e}")
        supabase = None


# --- Database Connection and Setup ---

def create_connection():
    """
    Create a PostgreSQL database connection using the Supabase DB URL.
    """
    try:
        conn = psycopg2.connect(SUPABASE_DB_URL)
        return conn
    except Exception as e:
        logging.error(f"❌ Database connection error: {e}")
        return None

# -----------------------------------------------------------------
# NOTE: All table creation, reset, and archive functions are REMOVED.
# This is now handled by manually running SQL in the Supabase dashboard
# and using pg_cron for cleanup.
# -----------------------------------------------------------------


# ========== USER SESSION OPERATIONS ==========

def set_session_state(user_id, state, order_id=None):
    """
    Sets the session state and current order ID for a user.
    Creates a new user record if it doesn't exist (UPSERT).
    """
    # Ensure user_id is a string, as it's TEXT PRIMARY KEY
    user_id = str(user_id)
    
    sql = """
        INSERT INTO users (id, session_state, current_order_id, last_active)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (id) 
        DO UPDATE SET
            session_state = EXCLUDED.session_state,
            current_order_id = EXCLUDED.current_order_id,
            last_active = NOW();
    """
    try:
        conn = create_connection()
        if not conn:
            return False
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, state, order_id))
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Error setting session state for {user_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_session_state(user_id):
    """Retrieves the current session state for a user."""
    sql = "SELECT session_state FROM users WHERE id = %s;"
    try:
        conn = create_connection()
        if not conn:
            return 'initial'
        
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
    try:
        conn = create_connection()
        if not conn:
            return None
        
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
    try:
        conn = create_connection()
        if not conn:
            return None
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(user_id),))
            phone = cursor.fetchone()
        
        return phone[0] if phone and phone[0] else None
    except Exception as e:
        logging.error(f"Error getting user phone for {user_id}: {e}")
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
    try:
        conn = create_connection()
        if not conn:
            return False
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(user_id), phone_number))
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Error updating phone number for {user_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

# ========== MENU OPERATIONS ==========

def get_menu():
    """Get all available menu items (including section)."""
    sql = "SELECT * FROM menu WHERE available = TRUE ORDER BY section, name;"
    try:
        conn = create_connection()
        if not conn:
            return []
        
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
    try:
        conn = create_connection()
        if not conn:
            return None
        
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
    """Add new menu item, or update price/section/availability if item already exists by name."""
    # Use case-insensitive conflict target
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
    try:
        conn = create_connection()
        if not conn:
            return "❌ Database connection error"
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (name, price, section))
            item_id = cursor.fetchone()[0]
            conn.commit()
        
        # The logic to check if it was an insert or update is complex.
        # Simplify the response.
        return f"✅ Synced '{name}' in *{section.title()}* for ₹{price:.2f} (ID: {item_id})"
        
    except Exception as e:
        logging.error(f"Error adding/updating menu item: {e}")
        return "❌ Error adding/updating menu item"
    finally:
        if conn:
            conn.close()

def update_menu_item(item_id, price):
    """Update menu item price by ID (traditional admin update)."""
    sql = "UPDATE menu SET price = %s WHERE id = %s RETURNING name;"
    try:
        conn = create_connection()
        if not conn:
            return "❌ Database connection error"
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (price, item_id))
            item = cursor.fetchone()
            conn.commit()
            
        if not item:
            return f"❌ Item ID {item_id} not found"
        
        return f"✅ Updated '{item[0]}' price to ₹{price:.2f}"
    except Exception as e:
        logging.error(f"Error updating menu item: {e}")
        return "❌ Error updating menu item"
    finally:
        if conn:
            conn.close()


def delete_menu_item(item_id):
    """Delete menu item (set as unavailable)."""
    sql = "UPDATE menu SET available = FALSE WHERE id = %s RETURNING name;"
    try:
        conn = create_connection()
        if not conn:
            return "❌ Database connection error"
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (item_id,))
            item = cursor.fetchone()
            conn.commit()
        
        if not item:
            return f"❌ Item ID {item_id} not found"
            
        return f"✅ Removed '{item[0]}' from menu"
    except Exception as e:
        logging.error(f"Error deleting menu item: {e}")
        return "❌ Error deleting menu item"
    finally:
        if conn:
            conn.close()

# ========== ORDER OPERATIONS ==========

def create_order(student_phone, order_details, total_amount, status='pending'):
    """Create a new order. Returns the new order ID."""
    # Convert items list to JSON string for storage
    items_json = json.dumps(order_details)
    sql = """
        INSERT INTO orders (student_phone, items, total_amount, status, created_at, updated_at)
        VALUES (%s, %s, %s, %s, NOW(), NOW())
        RETURNING id;
    """
    try:
        conn = create_connection()
        if not conn:
            return None
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(student_phone), items_json, total_amount, status))
            order_id = cursor.fetchone()[0]
            conn.commit()
            
        logging.info(f"Order {order_id} created for user {student_phone}")
        return order_id
    except Exception as e:
        logging.error(f"Error creating order: {e}")
        return None
    finally:
        if conn:
            conn.close()

def parse_order_items(items_json):
    """Parses the items JSON string into a Python list of dictionaries."""
    try:
        if isinstance(items_json, str):
            return json.loads(items_json)
        # If it's already a dict/list (from psycopg2 JSONB), just return it
        return items_json
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding order items JSON: {e}")
        return []
    except TypeError as e:
         logging.error(f"Error parsing order items (not a string?): {e}")
         return [] # Return empty list if it's None or invalid type

def get_order_details(order_id):
    """Get order details by ID."""
    sql = "SELECT * FROM orders WHERE id = %s;"
    try:
        conn = create_connection()
        if not conn:
            return None
        
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
    try:
        conn = create_connection()
        if not conn:
            return False
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (status, order_id))
            success = cursor.rowcount > 0
            conn.commit()
        
        if success:
            logging.info(f"Order {order_id} status updated to '{status}'")
        else:
            logging.warning(f"Order {order_id} not found for status update")
        return success
    except Exception as e:
        logging.error(f"Error updating order status: {e}")
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
    try:
        conn = create_connection()
        if not conn:
            return False
        
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
        logging.error(f"Error updating Razorpay details: {e}")
        return False
    finally:
        if conn:
            conn.close()


def update_order_cart(order_id, current_items, new_total):
    """Updates the items list and total amount for an ongoing order."""
    items_json = json.dumps(current_items)
    sql = """
        UPDATE orders
        SET items = %s,
            total_amount = %s,
            updated_at = NOW()
        WHERE id = %s;
    """
    try:
        conn = create_connection()
        if not conn:
            return False
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (items_json, new_total, order_id))
            success = cursor.rowcount > 0
            conn.commit()
        
        if success:
            logging.info(f"Order cart updated for order {order_id}. New Total: ₹{new_total:.2f}")
        return success
    except Exception as e:
        logging.error(f"Error updating order cart: {e}")
        return False
    finally:
        if conn:
            conn.close()


def update_order_service_type(order_id, service_type):
    """Updates the service type for an ongoing order."""
    sql = "UPDATE orders SET service_type = %s, updated_at = NOW() WHERE id = %s;"
    try:
        conn = create_connection()
        if not conn:
            return False
        
        with conn.cursor() as cursor:
            cursor.execute(sql, (service_type, order_id))
            success = cursor.rowcount > 0
            conn.commit()

        if success:
            logging.info(f"Service type updated for order {order_id}")
        return success
    except Exception as e:
        logging.error(f"Error updating service type: {e}")
        return False
    finally:
        if conn:
            conn.close()


def update_order_pickup_code(order_id, pickup_code):
    """Update pickup code for an order."""
    sql = "UPDATE orders SET pickup_code = %s, updated_at = NOW() WHERE id = %s;"
    try:
        conn = create_connection()
        if not conn:
            return False
        
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
        logging.error(f"Error updating pickup code: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_today_orders():
    """
    Retrieves all orders created during the current day from the live database.
    (Assumes DB is in UTC, adjusts to local time if needed, but NOW()::date is safer)
    """
    # This SQL query compares the 'created_at' timestamp (which is TIMESTAMPTZ)
    # to the current date in the database's timezone (usually UTC).
    sql = "SELECT * FROM orders WHERE created_at >= NOW()::date ORDER BY created_at ASC;"
    
    try:
        conn = create_connection()
        if not conn:
            return []
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute(sql)
            orders = [dict(row) for row in cursor.fetchall()]
        
        return orders
    except Exception as e:
        logging.error(f"Error fetching today's orders: {e}")
        return []
    finally:
        if conn:
            conn.close()

def get_order_statistics():
    """Calculates and returns key statistics for the admin dashboard."""
    stats = {}
    try:
        conn = create_connection()
        if not conn:
            return None
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            # 1. Total Orders and Revenue (only for 'paid' and 'delivered')
            cursor.execute("""
                SELECT COUNT(id), SUM(total_amount) 
                FROM orders 
                WHERE status IN ('paid', 'delivered');
            """)
            total_orders, total_revenue = cursor.fetchone()
            stats['total_orders'] = total_orders or 0
            stats['total_revenue'] = float(total_revenue or 0.0)

            # 2. Today's Orders (paid or delivered)
            cursor.execute("""
                SELECT COUNT(id) 
                FROM orders 
                WHERE created_at >= NOW()::date 
                AND status IN ('paid', 'delivered');
            """)
            stats['today_orders'] = cursor.fetchone()[0] or 0

            # 3. Orders by Status (all statuses)
            cursor.execute("SELECT status, COUNT(id) FROM orders GROUP BY status;")
            stats['status_counts'] = {row['status']: row['count'] for row in cursor.fetchall()}
        
        return stats
    except Exception as e:
        logging.error(f"Error getting order statistics: {e}")
        return None
    finally:
        if conn:
            conn.close()

# --- ARCHIVE/CLEANUP FUNCTIONS ---
# All archive/cleanup Python functions have been REMOVED.
# This logic is now handled by a pg_cron job in Supabase.
