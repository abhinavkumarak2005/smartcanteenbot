import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
import time
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Database configuration
BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / 'canteen_bot.db'
ARCHIVE_DIR = BASE_DIR / 'archives'
ARCHIVE_DIR.mkdir(exist_ok=True)  # Ensure archive directory exists


# --- Database Connection and Setup ---

def create_connection():
    """
    Create database connection with error handling and set timeout.
    Setting timeout to 10 seconds to solve the 'database is locked' error.
    """
    try:
        # Increase timeout from default (0) to 10 seconds
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        return None


def create_tables():
    """Create necessary database tables and perform migrations."""
    try:
        conn = create_connection()
        if not conn:
            return False

        cursor = conn.cursor()

        # Create menu table
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS menu
                       (
                           id
                           INTEGER
                           PRIMARY
                           KEY
                           AUTOINCREMENT,
                           name
                           TEXT
                           NOT
                           NULL,
                           price
                           REAL
                           NOT
                           NULL,
                           available
                           BOOLEAN
                           DEFAULT
                           1,
                           created_at
                           TIMESTAMP
                           DEFAULT
                           CURRENT_TIMESTAMP
                       )
                       ''')

        # Create orders table
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS orders
                       (
                           id
                           INTEGER
                           PRIMARY
                           KEY
                           AUTOINCREMENT,
                           student_phone
                           TEXT
                           NOT
                           NULL,
                           items
                           TEXT
                           NOT
                           NULL,
                           total_amount
                           REAL
                           NOT
                           NULL,
                           status
                           TEXT
                           DEFAULT
                           'pending',
                           payment_link
                           TEXT,
                           payment_expires_at
                           TEXT,
                           pickup_code
                           TEXT,
                           razorpay_order_id
                           TEXT,
                           service_type
                           TEXT,
                           created_at
                           TIMESTAMP
                           DEFAULT
                           CURRENT_TIMESTAMP,
                           updated_at
                           TIMESTAMP
                           DEFAULT
                           CURRENT_TIMESTAMP
                       )
                       ''')

        # Create user sessions/users table
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS users
                       (
                           id
                           TEXT
                           PRIMARY
                           KEY,
                           phone_number
                           TEXT,
                           session_state
                           TEXT
                           DEFAULT
                           'initial',
                           current_order_id
                           INTEGER,
                           created_at
                           TIMESTAMP
                           DEFAULT
                           CURRENT_TIMESTAMP,
                           last_active
                           TIMESTAMP
                           DEFAULT
                           CURRENT_TIMESTAMP
                       )
                       ''')

        # --- MIGRATIONS/COLUMN CHECKS ---
        cursor.execute("PRAGMA table_info(orders)")
        order_columns = [column[1] for column in cursor.fetchall()]
        if 'service_type' not in order_columns:
            cursor.execute('ALTER TABLE orders ADD COLUMN service_type TEXT')
            print("✅ Added 'service_type' column to orders table.")
        if 'razorpay_order_id' not in order_columns:
            cursor.execute('ALTER TABLE orders ADD COLUMN razorpay_order_id TEXT')
            print("✅ Added 'razorpay_order_id' column to orders table.")

        # Check and rename/add columns in user_sessions/users table
        cursor.execute("PRAGMA table_info(users)")
        user_columns = [column[1] for column in cursor.fetchall()]
        if 'phone_number' not in user_columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN phone_number TEXT')
                print("✅ Added 'phone_number' column to users table.")
            except sqlite3.OperationalError:
                pass

        conn.commit()
        conn.close()
        logging.info("Database tables created successfully!")
        return True

    except Exception as e:
        logging.error(f"Error creating tables: {e}")
        return False


def aggressive_db_reset():
    """
    FORCES a deletion of the database file. USE WITH CAUTION.
    This is necessary for Render deployment where shell commands fail.
    """
    if DATABASE_PATH.exists():
        try:
            os.remove(DATABASE_PATH)
            logging.warning(f"⚠️ Aggressively deleted old database file: {DATABASE_PATH}")
            return True
        except Exception as e:
            logging.error(f"❌ Failed to delete database file: {e}")
            return False
    return True # Return True if file didn't exist

def add_default_menu_items():
    """Add some default menu items if menu is empty."""
    try:
        conn = create_connection()
        if not conn:
            return False

        cursor = conn.cursor()

        # Check if menu is empty
        cursor.execute('SELECT COUNT(*) FROM menu')
        count = cursor.fetchone()[0]

        if count == 0:
            default_items = [
                ('Samosa', 15.0),
                ('Tea', 10.0),
                ('Coffee', 15.0),
                ('Sandwich', 25.0),
                ('Dosa', 30.0),
                ('Idli', 20.0),
                ('Vada', 15.0),
                ('Biriyani', 60.0),
                ('Paratha', 20.0),
                ('Lassi', 25.0)
            ]

            cursor.executemany('INSERT INTO menu (name, price) VALUES (?, ?)', default_items)
            conn.commit()
            conn.close()
            logging.info("Default menu items added successfully!")

        conn.close()
        return True

    except Exception as e:
        logging.error(f"Error adding default menu items: {e}")
        return False


# ========== MENU OPERATIONS (Unchanged) ==========

def get_menu():
    """Get all available menu items."""
    try:
        conn = create_connection()
        if not conn:
            return []

        cursor = conn.cursor()
        cursor.execute('SELECT * FROM menu WHERE available = 1 ORDER BY id')
        items = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return items

    except Exception as e:
        logging.error(f"Error getting menu: {e}")
        return []


def get_menu_item(item_id):
    """Get single menu item by ID."""
    try:
        conn = create_connection()
        if not conn:
            return None

        cursor = conn.cursor()
        cursor.execute('SELECT * FROM menu WHERE id = ? AND available = 1', (item_id,))
        item = cursor.fetchone()
        conn.close()
        return dict(item) if item else None

    except Exception as e:
        logging.error(f"Error getting menu item {item_id}: {e}")
        return None


def add_menu_item(name, price):
    """Add new menu item."""
    try:
        conn = create_connection()
        if not conn:
            return "❌ Database connection error"

        cursor = conn.cursor()
        cursor.execute('INSERT INTO menu (name, price) VALUES (?, ?)', (name, price))
        conn.commit()
        item_id = cursor.lastrowid
        conn.close()
        return f"✅ Added '{name}' for ₹{price:.2f} (ID: {item_id})"

    except Exception as e:
        logging.error(f"Error adding menu item: {e}")
        return "❌ Error adding menu item"


def update_menu_item(item_id, price):
    """Update menu item price."""
    try:
        conn = create_connection()
        if not conn:
            return "❌ Database connection error"

        cursor = conn.cursor()

        # First check if item exists
        cursor.execute('SELECT name FROM menu WHERE id = ?', (item_id,))
        item = cursor.fetchone()

        if not item:
            conn.close()
            return f"❌ Item ID {item_id} not found"

        # Update the price
        cursor.execute('UPDATE menu SET price = ? WHERE id = ?', (price, item_id))
        conn.commit()
        conn.close()
        return f"✅ Updated '{item[0]}' price to ₹{price:.2f}"

    except Exception as e:
        logging.error(f"Error updating menu item: {e}")
        return "❌ Error updating menu item"


def delete_menu_item(item_id):
    """Delete menu item (set as unavailable)."""
    try:
        conn = create_connection()
        if not conn:
            return "❌ Database connection error"

        cursor = conn.cursor()

        # First check if item exists
        cursor.execute('SELECT name FROM menu WHERE id = ?', (item_id,))
        item = cursor.fetchone()

        if not item:
            conn.close()
            return f"❌ Item ID {item_id} not found"

        # Set as unavailable instead of deleting
        cursor.execute('UPDATE menu SET available = 0 WHERE id = ?', (item_id,))
        conn.commit()
        conn.close()
        return f"✅ Removed '{item[0]}' from menu"

    except Exception as e:
        logging.error(f"Error deleting menu item: {e}")
        return "❌ Error deleting menu item"


# ========== ORDER OPERATIONS (Unchanged) ==========

def create_order(student_phone, order_details, total_amount, status='pending'):
    """Create a new order."""
    try:
        conn = create_connection()
        if not conn:
            return None

        cursor = conn.cursor()
        items_json = json.dumps(order_details)

        cursor.execute('''
                       INSERT INTO orders (student_phone, items, total_amount, status)
                       VALUES (?, ?, ?, ?)
                       ''', (student_phone, items_json, total_amount, status))

        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        logging.info(f"Order {order_id} created for user {student_phone}")
        return order_id

    except Exception as e:
        logging.error(f"Error creating order: {e}")
        return None


def get_order_details(order_id):
    """Get order details by ID."""
    try:
        conn = create_connection()
        if not conn:
            return None

        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
        order = cursor.fetchone()
        conn.close()
        return dict(order) if order else None

    except Exception as e:
        logging.error(f"Error getting order details for {order_id}: {e}")
        return None


def update_order_status(order_id, status):
    """Update order status."""
    try:
        conn = create_connection()
        if not conn:
            return False

        cursor = conn.cursor()
        cursor.execute('''
                       UPDATE orders
                       SET status     = ?,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?
                       ''', (status, order_id))

        success = cursor.rowcount > 0
        conn.commit()
        conn.close()

        if success:
            logging.info(f"Order {order_id} status updated to '{status}'")
        else:
            logging.warning(f"Order {order_id} not found for status update")

        return success

    except Exception as e:
        logging.error(f"Error updating order status: {e}")
        return False


def update_razorpay_details(order_id, razorpay_id, payment_link):
    """Updates the order with the Razorpay Payment Link ID and URL."""
    try:
        conn = create_connection()
        if not conn:
            return False

        cursor = conn.cursor()
        cursor.execute('''
                       UPDATE orders
                       SET razorpay_order_id = ?,
                           payment_link      = ?,
                           updated_at        = CURRENT_TIMESTAMP
                       WHERE id = ?
                       ''', (razorpay_id, payment_link, order_id))

        success = cursor.rowcount > 0
        conn.commit()
        conn.close()

        if success:
            logging.info(f"Razorpay details updated for order {order_id}")
        else:
            logging.warning(f"Order {order_id} not found for Razorpay update")

        return success

    except Exception as e:
        logging.error(f"Error updating Razorpay details: {e}")
        return False


def update_order_cart(order_id, current_items, new_total):
    """Updates the items list and total amount for an ongoing order."""
    try:
        conn = create_connection()
        if not conn:
            return False

        cursor = conn.cursor()
        items_json = json.dumps(current_items)

        cursor.execute('''
                       UPDATE orders
                       SET items        = ?,
                           total_amount = ?,
                           updated_at   = CURRENT_TIMESTAMP
                       WHERE id = ?
                       ''', (items_json, new_total, order_id))

        success = cursor.rowcount > 0
        conn.commit()
        conn.close()

        if success:
            logging.info(f"Order cart updated for order {order_id}. New Total: ₹{new_total:.2f}")
        return success
    except Exception as e:
        logging.error(f"Error updating order cart: {e}")
        return False


def update_order_service_type(order_id, service_type):
    """Updates the service type for an ongoing order."""
    try:
        conn = create_connection()
        if not conn:
            return False

        cursor = conn.cursor()

        cursor.execute('''
                       UPDATE orders
                       SET service_type = ?,
                           updated_at   = CURRENT_TIMESTAMP
                       WHERE id = ?
                       ''', (service_type, order_id))

        success = cursor.rowcount > 0
        conn.commit()
        conn.close()

        if success:
            logging.info(f"Service type updated for order {order_id}")
        return success

    except Exception as e:
        logging.error(f"Error updating service type: {e}")
        return False


def update_order_pickup_code(order_id, pickup_code):
    """Update pickup code for an order."""
    try:
        conn = create_connection()
        if not conn:
            return False

        cursor = conn.cursor()
        cursor.execute('''
                       UPDATE orders
                       SET pickup_code = ?,
                           updated_at  = CURRENT_TIMESTAMP
                       WHERE id = ?
                       ''', (pickup_code, order_id))

        success = cursor.rowcount > 0
        conn.commit()
        conn.close()

        if success:
            logging.info(f"Pickup code updated for order {order_id}")
        else:
            logging.warning(f"Order {order_id} not found for pickup code update")

        return success

    except Exception as e:
        logging.error(f"Error updating pickup code: {e}")
        return False


def get_recent_orders(limit=10):
    """Get recent orders for admin."""
    try:
        conn = create_connection()
        if not conn:
            return []

        cursor = conn.cursor()
        cursor.execute('''
                       SELECT *
                       FROM orders
                       ORDER BY created_at DESC LIMIT ?
                       ''', (limit,))

        orders = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return orders

    except Exception as e:
        logging.error(f"Error getting recent orders: {e}")
        return []


def get_today_orders():
    """
    Retrieves all orders created during the current day from the live database.
    """
    # FIX: Using Python to define the cutoff times ensures consistency regardless of database timezone settings.
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')

    try:
        conn = create_connection()
        if not conn:
            return []

        cursor = conn.cursor()
        cursor.execute('''
                       SELECT *
                       FROM orders
                       WHERE created_at >= ?
                       ORDER BY created_at ASC
                       ''', (today_start,))

        orders = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return orders

    except Exception as e:
        logging.error(f"Error fetching today's orders: {e}")
        return []


def get_orders_for_day(target_date: datetime):
    """
    Fetches all orders created on the given target_date (YYYY-MM-DD).
    Returns a list of dictionaries.
    """
    try:
        conn = create_connection()
        if not conn:
            return []

        # Define the start and end of the target day
        start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
        end_of_day = (target_date + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).strftime(
            '%Y-%m-%d %H:%M:%S')

        cursor = conn.cursor()
        cursor.execute('''
                       SELECT *
                       FROM orders
                       WHERE created_at >= ?
                         AND created_at < ?
                       ORDER BY created_at ASC
                       ''', (start_of_day, end_of_day))

        orders = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return orders

    except Exception as e:
        logging.error(f"Error fetching orders for day {target_date.date()}: {e}")
        return []


def get_all_orders_up_to_date(cutoff_date: datetime):
    """
    Fetches all orders created up to (but NOT including) the specified cutoff date (midnight).
    """
    try:
        conn = create_connection()
        if not conn:
            return []

        # The cutoff_date should be formatted to the highest precision needed
        cutoff_str = cutoff_date.strftime('%Y-%m-%d %H:%M:%S')

        cursor = conn.cursor()
        cursor.execute('''
                       SELECT *
                       FROM orders
                       WHERE created_at < ?
                       ORDER BY created_at ASC
                       ''', (cutoff_str,))

        orders = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return orders

    except Exception as e:
        logging.error(f"Error fetching orders up to {cutoff_date.date()}: {e}")
        return None


def delete_all_orders_up_to_date(cutoff_date: datetime):
    """
    Deletes all orders created up to (but NOT including) the specified cutoff date (midnight).
    Also resets the order ID counter.
    """
    try:
        conn = create_connection()
        if not conn:
            return 0

        # Delete all orders strictly older than the cutoff_date's midnight.
        cutoff_str = cutoff_date.strftime('%Y-%m-%d %H:%M:%S')

        cursor = conn.cursor()

        # 1. DELETE the old orders
        cursor.execute('''
                       DELETE
                       FROM orders
                       WHERE created_at < ?
                       ''', (cutoff_str,))

        deleted_count = cursor.rowcount

        # 2. CRITICAL FIX: Reset the SQLite sequence counter if records were deleted
        if deleted_count > 0:
            cursor.execute('''
                               UPDATE sqlite_sequence
                               SET seq = 0
                               WHERE name = 'orders'
                               ''')

        conn.commit()
        conn.close()
        return deleted_count

    except Exception as e:
        logging.error(f"Error deleting orders up to {cutoff_date.date()}: {e}")
        return 0


# New helper function for archiving
def save_orders_to_file(orders, cutoff_date_str):
    """Saves a list of orders to a JSON file, named by the cutoff date."""
    if not orders:
        return 0

    try:
        ARCHIVE_DIR.mkdir(exist_ok=True)
        # Archive file named after the date of the *cutoff*
        filename = f"orders_archived_before_{cutoff_date_str}.json"
        filepath = ARCHIVE_DIR / filename

        # Convert Row objects to serializable dictionaries
        serializable_orders = []
        for order in orders:
            order_dict = dict(order)
            # Ensure items are stored as parsed objects, not strings, in the archive file
            order_dict['items'] = parse_order_items(order_dict['items'])
            serializable_orders.append(order_dict)

        # Use default=str to safely serialize datetime objects
        with open(filepath, 'w') as f:
            json.dump(serializable_orders, f, indent=4, default=str)

        logging.info(f"Successfully archived {len(orders)} orders to {filepath.name}")
        return len(orders)

    except Exception as e:
        logging.error(f"Error saving orders to archive file: {e}")
        return 0


def get_archived_orders_by_filename(filename):
    """Reads and returns order data from a specific archived JSON file."""
    filepath = ARCHIVE_DIR / filename

    if not filepath.exists():
        logging.warning(f"Archive file not found: {filename}")
        return None

    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
            return data
        except json.JSONDecodeError as e:
            logging.error(f"Error decoding JSON from archive file {filename}: {e}")
            return None
        except Exception as e:
            logging.error(f"Error reading archived file {filename}: {e}")
            return None


def get_archive_file_list():
    """
    Returns a list of all archive filenames in the archive directory.
    FIX: Correctly formats the button text to show the date of the contained data.
    """
    try:
        ARCHIVE_DIR.mkdir(exist_ok=True)  # Ensure directory exists before checking contents
        if not ARCHIVE_DIR.exists():
            return []

        # List all valid archive files
        files = [f.name for f in ARCHIVE_DIR.iterdir() if
                 f.is_file() and f.name.startswith('orders_archived_before_') and f.name.endswith('.json')]

        # We need to process the list to change the display date
        processed_files = []
        for filename in files:
            # Filename format: orders_archived_before_YYYY-MM-DD.json
            date_part_str = filename.replace('orders_archived_before_', '').replace('.json', '')

            try:
                # Convert the cutoff date (e.g., 2025-10-02) to a datetime object
                cutoff_date = datetime.strptime(date_part_str, '%Y-%m-%d')

                # The data contained in this file is from the day *before* the cutoff.
                # Example: File named *before_2025-10-02* contains data from *2025-10-01*.
                data_date = cutoff_date - timedelta(days=1)

                processed_files.append({
                    'filename': filename,
                    'sort_key': data_date
                })
            except ValueError:
                # Handle corrupted filenames by skipping them
                logging.warning(f"Skipping malformed archive filename: {filename}")
                continue

        # Sort files by the data date (most recent first)
        processed_files.sort(key=lambda x: x['sort_key'], reverse=True)

        # Return only the filename to the caller (app.py will handle formatting the final button)
        return [f['filename'] for f in processed_files]


    except Exception as e:
        logging.error(f"Error reading archive directory: {e}")
        return []


def clear_active_sessions_after_reset():
    """
    Clears active user sessions (those pointing to an order ID)
    to prevent them from linking to a newly created order with the same ID.
    Only clears sessions that are NOT 'initial' and NOT 'pickup_ready'.
    """
    try:
        conn = create_connection()
        if not conn:
            return 0

        cursor = conn.cursor()

        # Reset sessions actively involved in ordering
        cursor.execute('''
                       UPDATE users
                       SET session_state    = 'initial',
                           current_order_id = NULL,
                           last_active      = CURRENT_TIMESTAMP
                       WHERE session_state != 'initial' 
            AND session_state NOT LIKE 'pickup_%' 
            AND session_state NOT LIKE 'waiting_%'
                       ''')

        cleared_count = cursor.rowcount
        conn.commit()
        conn.close()

        if cleared_count > 0:
            logging.info(f"🧹 Cleared {cleared_count} active ordering sessions due to daily order ID reset.")

        return cleared_count

    except Exception as e:
        logging.error(f"Error clearing active sessions after reset: {e}")
        return 0


def archive_and_reset_daily_orders():
    """
    Checks for orders created YESTERDAY or earlier. If found, archives them, deletes them,
    and resets the order ID sequence for the current day.
    """
    # The cutoff date is TODAY's midnight. We archive everything BEFORE this time.
    today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # The name of the file will reflect the date of the cutoff
    cutoff_date_str = today_midnight.strftime('%Y-%m-%d')

    # 1. Fetch all orders strictly OLDER than today's midnight
    orders_to_archive = get_all_orders_up_to_date(today_midnight)

    if not orders_to_archive:
        logging.info("✅ No past-day orders found to archive.")
        return 0

    logging.warning(f"⚠️ Found {len(orders_to_archive)} orders created before {cutoff_date_str} to archive.")

    # 2. Archive the old orders
    archived_count = save_orders_to_file(orders_to_archive, cutoff_date_str)

    if archived_count > 0:
        # 3. Delete the archived orders and reset the sequence
        deleted_count = delete_all_orders_up_to_date(today_midnight)

        # 4. Clear related user sessions
        clear_active_sessions_after_reset()

        logging.info(f"🗑️ Successfully deleted {deleted_count} archived orders and reset order ID counter.")
        return deleted_count

    else:
        logging.error("❌ Archiving failed, aborting deletion and reset.")
        return 0


# --- User Session/Phone Management ---

def update_user_phone(student_id, phone_number):
    """Stores the collected phone number in the users table."""
    try:
        conn = create_connection()
        if not conn:
            return False

        cursor = conn.cursor()

        # 1. Get current state/order (if user exists)
        cursor.execute("SELECT session_state, current_order_id FROM users WHERE id = ?", (student_id,))
        result = cursor.fetchone()

        state = result['session_state'] if result else 'initial'
        order_id = result['current_order_id'] if result else None

        # 2. INSERT OR REPLACE with the new phone number, keeping old state/order_id
        cursor.execute('''
            INSERT OR REPLACE INTO users 
            (id, phone_number, session_state, current_order_id, last_active)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (student_id, phone_number, state, order_id))

        conn.commit()
        conn.close()
        logging.info(f"Phone number updated for user {student_id}")
        return True

    except Exception as e:
        logging.error(f"Error updating phone number: {e}")
        return False


def get_user_phone(student_id):
    """Retrieves the stored phone number for the user."""
    try:
        conn = create_connection()
        if not conn:
            return None

        cursor = conn.cursor()
        cursor.execute('SELECT phone_number FROM users WHERE id = ?', (student_id,))
        result = cursor.fetchone()
        conn.close()
        return result['phone_number'] if result and result['phone_number'] else None

    except Exception as e:
        logging.error(f"Error retrieving phone number: {e}")
        return None


# ========== SESSION MANAGEMENT (using 'users' table) ==========

def get_session_state(user_id):
    """Get user session state."""
    MAX_RETRIES = 5
    for attempt in range(MAX_RETRIES):
        try:
            conn = create_connection()
            if not conn:
                return 'initial'

            cursor = conn.cursor()
            # Ensure user exists first, if not, create a basic entry
            cursor.execute("INSERT OR IGNORE INTO users (id, session_state) VALUES (?, ?)",
                           (user_id, 'initial'))
            conn.commit()  # Commit the insert if it happened

            cursor.execute('SELECT session_state FROM users WHERE id = ?', (user_id,))
            result = cursor.fetchone()
            conn.close()

            return result['session_state'] if result else 'initial'

        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e) and attempt < MAX_RETRIES - 1:
                time.sleep(0.2)
            else:
                logging.error(f"Final Error getting session state: {e}")
                return 'initial'
        except Exception as e:
            logging.error(f"Error getting session state: {e}")
            return 'initial'


def set_session_state(user_id, state, order_id=None):
    """Set user session state with a retry mechanism for database locks."""
    MAX_RETRIES = 5
    for attempt in range(MAX_RETRIES):
        try:
            conn = create_connection()
            if not conn:
                return False

            cursor = conn.cursor()

            # Use a two-step approach for robustness, ensuring the user exists first
            cursor.execute("INSERT OR IGNORE INTO users (id, session_state) VALUES (?, ?)",
                           (user_id, 'initial'))

            cursor.execute('''
                           UPDATE users
                           SET session_state    = ?,
                               current_order_id = ?,
                               last_active      = CURRENT_TIMESTAMP
                           WHERE id = ?
                           ''', (state, order_id, user_id))

            conn.commit()
            conn.close()
            return True

        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e) and attempt < MAX_RETRIES - 1:
                time.sleep(0.3)
            else:
                logging.error(f"Final Error setting session state: {e}")
                return False
        except Exception as e:
            logging.error(f"Error setting session state: {e}")
            return False


def get_session_order_id(user_id):
    """Get current order ID from session."""
    MAX_RETRIES = 5
    for attempt in range(MAX_RETRIES):
        try:
            conn = create_connection()
            if not conn:
                return None

            cursor = conn.cursor()
            cursor.execute('SELECT current_order_id FROM users WHERE id = ?', (user_id,))
            result = cursor.fetchone()
            conn.close()

            return result['current_order_id'] if result and result['current_order_id'] is not None else None

        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e) and attempt < MAX_RETRIES - 1:
                time.sleep(0.2)
            else:
                logging.error(f"Final Error getting session order ID: {e}")
                return None
        except Exception as e:
            logging.error(f"Error getting session order ID: {e}")
            return None


def cleanup_old_sessions(days_old=7):
    """Clean up old user sessions."""
    try:
        conn = create_connection()
        if not conn:
            return False

        cursor = conn.cursor()
        # Ensure proper syntax for date calculation
        cursor.execute('''
            DELETE FROM users 
            WHERE last_active < datetime('now', '-%d days')
        ''' % days_old)

        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()

        if deleted_count > 0:
            logging.info(f"🧹 Cleaned up {deleted_count} old sessions")

        return True

    except Exception as e:
        logging.error(f"Error cleaning up old sessions: {e}")
        return False


# --- UTILITY FUNCTION FOR ORDER ITEMS ---
def parse_order_items(items_json):
    """Parse order items from JSON string."""
    try:
        if isinstance(items_json, str) and items_json:
            return json.loads(items_json)
        elif isinstance(items_json, list):
            return items_json
        else:
            return []
    except (json.JSONDecodeError, TypeError):
        return []
