# db_manager.py - Database Manager for Telegram Canteen Bot (Supabase/PostgreSQL Version)

import psycopg2
from psycopg2.extras import DictCursor
import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

# --- CONFIGURATION ---
BASE_DIR = Path(__file__).resolve().parent
DOTENV_PATH = BASE_DIR / '.env'
load_dotenv(dotenv_path=DOTENV_PATH)

# Supabase PostgreSQL Connection URL
SUPABASE_DB_URL = os.getenv('SUPABASE_DB_URL')

import socket
from urllib.parse import urlparse, urlunparse

def create_connection():
    """Create PostgreSQL database connection with forced IPv4 resolution."""
    try:
        if not SUPABASE_DB_URL:
             print("❌ SUPABASE_DB_URL is not set.")
             return None
        
        # Parse the URL
        parsed = urlparse(SUPABASE_DB_URL)
        hostname = parsed.hostname
        
        # Resolve to IPv4 (AF_INET)
        # Vercel/Supabase often fail on IPv6, so we force IPv4
        try:
            ipv4_address = socket.gethostbyname(hostname)
            # Reconstruct URL with IP address
            # We must keep the port and credentials
            new_netloc = parsed.netloc.replace(hostname, ipv4_address)
            final_url = urlunparse(parsed._replace(netloc=new_netloc))
        except Exception as dns_error:
            print(f"⚠️ DNS Resolution failed, trying original URL: {dns_error}")
            final_url = SUPABASE_DB_URL

        conn = psycopg2.connect(final_url)
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        return None

def create_tables():
    """Create necessary database tables (PostgreSQL compatible)."""
    try:
        conn = create_connection()
        if not conn:
            return False

        with conn.cursor() as cursor:
            # 1. Users Table (New V2 feature)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    name TEXT,
                    phone_number TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')

            # 2. Menu Table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS menu (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    available BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')

            # 3. Orders Table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    student_phone TEXT, -- Keeping for backward compatibility
                    user_id BIGINT,     -- Link to users table
                    items JSONB NOT NULL,
                    total_amount REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    payment_link TEXT,
                    payment_expires_at TIMESTAMP,
                    pickup_code TEXT,
                    razorpay_order_id TEXT,
                    daily_token INTEGER, -- Token number for the day
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            
            # Apply Schema Updates for existing tables (safe migration)
            try:
                cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS daily_token INTEGER;")
                cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_id BIGINT;")
            except Exception as ex:
                print(f"⚠️ Schema update notice: {ex}")
                conn.rollback() # Rollback the failed alter, but continue (if it failed it likely exists or syntax error)
                # But we should probably not fail the whole transaction? 
                # Postgres transaction will abort if error. We need savepoints or just ignore.
                # Actually, `ADD COLUMN IF NOT EXISTS` is supported in Postgres 9.6+. Supabase is 15+.
                pass

            # 4. User Sessions Table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_sessions (
                    student_phone TEXT PRIMARY KEY,
                    state TEXT DEFAULT 'initial',
                    current_order_id INTEGER,
                    cart JSONB DEFAULT '[]', -- New: Cart support
                    registration_data JSONB DEFAULT '{}', -- New: Temp reg data
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            
            # Apply Session Schema Updates
            try:
                cursor.execute("ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS cart JSONB DEFAULT '[]';")
                cursor.execute("ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS registration_data JSONB DEFAULT '{}';")
            except: pass

            conn.commit()
            print("✅ Database tables created/verified successfully (PostgreSQL)!")
            return True

    except Exception as e:
        print(f"❌ Error creating tables: {e}")
        return False
    finally:
        if conn: conn.close()

def add_default_menu_items():
    """Add some default menu items if menu is empty."""
    try:
        conn = create_connection()
        if not conn: return False

        with conn.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM menu')
            count = cursor.fetchone()[0]

            if count == 0:
                default_items = [
                    ('Samosa', 15.0),
                    ('Tea', 10.0),
                    ('Coffee', 15.0),
                ]
                cursor.executemany('INSERT INTO menu (name, price) VALUES (%s, %s)', default_items)
                conn.commit()
                print("✅ Default menu items added successfully!")

        return True
    except Exception as e:
        print(f"❌ Error adding default menu items: {e}")
        return False
    finally:
        if conn: conn.close()

# ========== MENU OPERATIONS ==========

# ========== MENU OPERATIONS ==========

def get_menu(conn=None):
    """Get all available menu items."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return []

    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('SELECT * FROM menu WHERE available = TRUE ORDER BY id')
            items = [dict(row) for row in cursor.fetchall()]
        return items
    except Exception as e:
        print(f"❌ Error getting menu: {e}")
        return []
    finally:
        if should_close and conn: conn.close()

def get_menu_item(item_id, conn=None):
    """Get single menu item by ID."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return None

    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('SELECT * FROM menu WHERE id = %s AND available = TRUE', (item_id,))
            item = cursor.fetchone()
        return dict(item) if item else None
    except Exception as e:
        print(f"❌ Error getting menu item {item_id}: {e}")
        return None
    finally:
        if should_close and conn: conn.close()

def add_menu_item(name, price):
    """Add new menu item."""
    try:
        conn = create_connection()
        if not conn: return "❌ Database connection error"

        with conn.cursor() as cursor:
            cursor.execute('INSERT INTO menu (name, price) VALUES (%s, %s) RETURNING id', (name, price))
            item_id = cursor.fetchone()[0]
            conn.commit()
        return f"✅ Added '{name}' for ₹{price:.2f} (ID: {item_id})"

    except Exception as e:
        print(f"❌ Error adding menu item: {e}")
        return "❌ Error adding menu item"
    finally:
        if conn: conn.close()

def update_menu_item(item_id, price):
    """Update menu item price."""
    try:
        conn = create_connection()
        if not conn: return "❌ Database connection error"

        with conn.cursor() as cursor:
            cursor.execute('UPDATE menu SET price = %s WHERE id = %s RETURNING name', (price, item_id))
            item = cursor.fetchone()
            conn.commit()
            
            if not item:
                return f"❌ Item ID {item_id} not found"
            return f"✅ Updated '{item[0]}' price to ₹{price:.2f}"

    except Exception as e:
        print(f"❌ Error updating menu item: {e}")
        return "❌ Error updating menu item"
    finally:
        if conn: conn.close()

def delete_menu_item(item_id):
    """Delete menu item (set as unavailable)."""
    try:
        conn = create_connection()
        if not conn: return "❌ Database connection error"

        with conn.cursor() as cursor:
            cursor.execute('UPDATE menu SET available = FALSE WHERE id = %s RETURNING name', (item_id,))
            item = cursor.fetchone()
            conn.commit()

            if not item:
                return f"❌ Item ID {item_id} not found"
            return f"✅ Removed '{item[0]}' from menu"
    except Exception as e:
        print(f"❌ Error deleting menu item: {e}")
        return "❌ Error deleting menu item"
    finally:
        if conn: conn.close()

# ========== ORDER OPERATIONS ==========

def create_order(student_phone, order_details, total_amount, status='pending', conn=None, user_id=None):
    """Create a new order with daily token."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return None
        
    try:
        # Postgres JSONB handles list/dict directly
        items_json = json.dumps(order_details)

        with conn.cursor() as cursor:
            # Generate Daily Token (Count today's orders + 1)
            # We use Postgres 'limit' or 'count' effectively.
            # Ideally this should be an atomic counter or sequence, but count is fine for this scale.
            cursor.execute("SELECT COUNT(*) FROM orders WHERE created_at::date = CURRENT_DATE")
            count = cursor.fetchone()[0]
            daily_token = count + 1

            cursor.execute('''
                INSERT INTO orders (student_phone, user_id, items, total_amount, status, daily_token)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            ''', (student_phone, user_id, items_json, total_amount, status, daily_token))
            
            order_id = cursor.fetchone()[0]
            conn.commit()
            
        print(f"✅ Order {order_id} created (Token #{daily_token})")
        return order_id
    except Exception as e:
        print(f"❌ Error creating order: {e}")
        return None
    finally:
        if should_close and conn: conn.close()

def get_order_details(order_id, conn=None):
    """Get order details by ID."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return None

    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
            order = cursor.fetchone()
        return dict(order) if order else None
    except Exception as e:
        print(f"❌ Error getting order details for {order_id}: {e}")
        return None
    finally:
        if should_close and conn: conn.close()

def get_order_by_razorpay_order_id(razorpay_order_id):
    """Get order details by Razorpay Order ID."""
    try:
        conn = create_connection()
        if not conn: return None

        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('SELECT * FROM orders WHERE razorpay_order_id = %s', (razorpay_order_id,))
            order = cursor.fetchone()
        return dict(order) if order else None
    except Exception as e:
        print(f"❌ Error getting order by Razorpay ID: {e}")
        return None
    finally:
        if conn: conn.close()

def update_order_status(order_id, status, conn=None):
    """Update order status."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return False

    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                UPDATE orders SET status = %s, updated_at = CURRENT_TIMESTAMP 
                WHERE id = %s
            ''', (status, order_id))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"❌ Error updating order status: {e}")
        return False
    finally:
        if should_close and conn: conn.close()

def update_order_razorpay_id(order_id, razorpay_id):
    """Update Razorpay Order ID."""
    try:
        conn = create_connection()
        if not conn: return False

        with conn.cursor() as cursor:
            cursor.execute('''
                UPDATE orders SET razorpay_order_id = %s, updated_at = CURRENT_TIMESTAMP 
                WHERE id = %s
            ''', (razorpay_id, order_id))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"❌ Error updating Razorpay ID: {e}")
        return False
    finally:
        if conn: conn.close()

def update_order_pickup_code(order_id, pickup_code):
    """Update pickup code for an order."""
    try:
        conn = create_connection()
        if not conn: return False

        with conn.cursor() as cursor:
            cursor.execute('''
                UPDATE orders SET pickup_code = %s, updated_at = CURRENT_TIMESTAMP 
                WHERE id = %s
            ''', (pickup_code, order_id))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"❌ Error updating pickup code: {e}")
        return False
    finally:
        if conn: conn.close()

def get_recent_orders(limit=10):
    """Get recent orders for admin."""
    try:
        conn = create_connection()
        if not conn: return []

        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('''
                SELECT * FROM orders 
                ORDER BY created_at DESC 
                LIMIT %s
            ''', (limit,))
            orders = [dict(row) for row in cursor.fetchall()]
        return orders
    except Exception as e:
        print(f"❌ Error getting recent orders: {e}")
        return []
    finally:
        if conn: conn.close()

def parse_order_items(items_input):
    """Parse order items from JSON string or return if already list."""
    try:
        if isinstance(items_input, str):
            return json.loads(items_input)
        return items_input
    except Exception as e:
        print(f"❌ Error parsing order items: {e}")
        return []

# ========== SESSION MANAGEMENT ==========

def set_session_state(student_phone, state, order_id=None, conn=None):
    """Set user session state (Upsert)."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return False
        
    try:
        student_phone = str(student_phone)

        with conn.cursor() as cursor:
            cursor.execute('''
                INSERT INTO user_sessions (student_phone, state, current_order_id, updated_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (student_phone) 
                DO UPDATE SET state = EXCLUDED.state, 
                              current_order_id = EXCLUDED.current_order_id,
                              updated_at = EXCLUDED.updated_at
            ''', (student_phone, state, order_id))
            conn.commit()
        return True
    except Exception as e:
        print(f"❌ Error setting session state: {e}")
        return False
    finally:
        if should_close and conn: conn.close()

def get_session_state(student_phone, conn=None):
    """Get user session state."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return 'initial'

    try:
        student_phone = str(student_phone)
        with conn.cursor() as cursor:
            cursor.execute('SELECT state FROM user_sessions WHERE student_phone = %s', (student_phone,))
            result = cursor.fetchone()
        return result[0] if result else 'initial'
    except Exception as e:
        print(f"❌ Error getting session state: {e}")
        return 'initial'
    finally:
        if should_close and conn: conn.close()

def get_session_order_id(student_phone, conn=None):
    """Get current order ID from session."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return None
        
    try:
        student_phone = str(student_phone)

        with conn.cursor() as cursor:
            cursor.execute('SELECT current_order_id FROM user_sessions WHERE student_phone = %s', (student_phone,))
            result = cursor.fetchone()
        return result[0] if result else None
    except Exception as e:
        print(f"❌ Error getting session order ID: {e}")
        return None
    finally:
        if should_close and conn: conn.close()

# ========== USER OPERATIONS (V2) ==========

def get_user(telegram_id, conn=None):
    """Get user profile by Telegram ID."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return None

    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('SELECT * FROM users WHERE telegram_id = %s', (telegram_id,))
            user = cursor.fetchone()
        return dict(user) if user else None
    except Exception as e:
        print(f"❌ Error getting user {telegram_id}: {e}")
        return None
    finally:
        if should_close and conn: conn.close()

def register_user(telegram_id, name, phone, conn=None):
    """Register a new user or update existing."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return False

    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                INSERT INTO users (telegram_id, name, phone_number)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE 
                SET name = EXCLUDED.name, phone_number = EXCLUDED.phone_number
            ''', (telegram_id, name, phone))
            conn.commit()
        return True
    except Exception as e:
        print(f"❌ Error registering user: {e}")
        return False
    finally:
        if should_close and conn: conn.close()

def set_session_data(student_phone, data_type, value, conn=None):
    """Update specific session data (cart, reg_data)."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return False

    try:
        student_phone = str(student_phone)
        col_name = 'cart' if data_type == 'cart' else 'registration_data'
        value_json = json.dumps(value)

        with conn.cursor() as cursor:
             # Ensure session exists first
            cursor.execute('''
                INSERT INTO user_sessions (student_phone, updated_at)
                VALUES (%s, CURRENT_TIMESTAMP)
                ON CONFLICT (student_phone) DO NOTHING
            ''', (student_phone,))

            cursor.execute(f'''
                UPDATE user_sessions SET {col_name} = %s, updated_at = CURRENT_TIMESTAMP
                WHERE student_phone = %s
            ''', (value_json, student_phone))
            conn.commit()
            return True
    except Exception as e:
        print(f"❌ Error setting session data {data_type}: {e}")
        return False
    finally:
        if should_close and conn: conn.close()

def get_session_data(student_phone, data_type, conn=None):
    """Get specific session data."""
    should_close = False
    if not conn:
        conn = create_connection()
        should_close = True
        if not conn: return [] if data_type == 'cart' else {}

    try:
        col_name = 'cart' if data_type == 'cart' else 'registration_data'
        with conn.cursor() as cursor:
            cursor.execute(f'SELECT {col_name} FROM user_sessions WHERE student_phone = %s', (str(student_phone),))
            res = cursor.fetchone()
            if res and res[0]:
                return res[0]
            return [] if data_type == 'cart' else {}
    except Exception as e:
        print(f"❌ Error getting session data: {e}")
        return [] if data_type == 'cart' else {}
    finally:
        if should_close and conn: conn.close()

# ========== STATISTICS & CLEANUP ==========

def get_order_statistics():
    """Get statistics (PostgreSQL compatible)."""
    try:
        conn = create_connection()
        if not conn: return {}

        with conn.cursor(cursor_factory=DictCursor) as cursor:
            # Stats
            cursor.execute('SELECT COUNT(*) as count FROM orders')
            total_orders = cursor.fetchone()['count']
            
            cursor.execute("SELECT SUM(total_amount) as rev FROM orders WHERE status = 'paid'")
            total_revenue = cursor.fetchone()['rev'] or 0.0

            cursor.execute("SELECT status, COUNT(*) as count FROM orders GROUP BY status")
            status_counts = {row['status']: row['count'] for row in cursor.fetchall()}
            
            cursor.execute("SELECT COUNT(*) as count FROM orders WHERE created_at >= CURRENT_DATE")
            today_orders = cursor.fetchone()['count']

        return {
            'total_orders': total_orders,
            'total_revenue': total_revenue,
            'today_orders': today_orders,
            'status_counts': status_counts
        }

    except Exception as e:
        print(f"❌ Error getting statistics: {e}")
        return {}
    finally:
        if conn: conn.close()

def cleanup_old_sessions(days_old=7):
    """Cleanup old sessions."""
    try:
        conn = create_connection()
        if not conn: return False
        
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM user_sessions WHERE updated_at < NOW() - INTERVAL '%s days'", (days_old,))
            conn.commit()
            print(f"🧹 Sessions cleanup run.")
        return True
    except Exception as e:
        print(f"❌ Error cleaning up: {e}")
        return False
    finally:
        if conn: conn.close()

def test_database_operations():
    """Test connection."""
    print("Testing DB connection...")
    conn = create_connection()
    if conn:
        print("✅ Connection successful")
        conn.close()
        return True
    return False

# For direct execution testing
if __name__ == '__main__':
    create_tables()
    add_default_menu_items()
    test_database_operations()
