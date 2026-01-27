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

def create_connection():
    """Create PostgreSQL database connection."""
    try:
        if not SUPABASE_DB_URL:
             # Fallback or error if not set
             print("❌ SUPABASE_DB_URL is not set.")
             return None
        conn = psycopg2.connect(SUPABASE_DB_URL)
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
            # Create menu table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS menu (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    available BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')

            # Create orders table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    student_phone TEXT NOT NULL,
                    items JSONB NOT NULL,
                    total_amount REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    payment_link TEXT,
                    payment_expires_at TIMESTAMP,
                    pickup_code TEXT,
                    razorpay_order_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')

            # Create user sessions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_sessions (
                    student_phone TEXT PRIMARY KEY,
                    state TEXT DEFAULT 'initial',
                    current_order_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')

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

def get_menu():
    """Get all available menu items."""
    try:
        conn = create_connection()
        if not conn: return []

        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('SELECT * FROM menu WHERE available = TRUE ORDER BY id')
            items = [dict(row) for row in cursor.fetchall()]
        return items
    except Exception as e:
        print(f"❌ Error getting menu: {e}")
        return []
    finally:
        if conn: conn.close()

def get_menu_item(item_id):
    """Get single menu item by ID."""
    try:
        conn = create_connection()
        if not conn: return None

        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('SELECT * FROM menu WHERE id = %s AND available = TRUE', (item_id,))
            item = cursor.fetchone()
        return dict(item) if item else None
    except Exception as e:
        print(f"❌ Error getting menu item {item_id}: {e}")
        return None
    finally:
        if conn: conn.close()

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

def create_order(student_phone, order_details, total_amount, status='pending'):
    """Create a new order."""
    try:
        conn = create_connection()
        if not conn: return None
        
        # Postgres JSONB handles list/dict directly if adapter is registered,
        # but json.dumps is safer for compatibility.
        items_json = json.dumps(order_details)

        with conn.cursor() as cursor:
            cursor.execute('''
                INSERT INTO orders (student_phone, items, total_amount, status)
                VALUES (%s, %s, %s, %s) RETURNING id
            ''', (student_phone, items_json, total_amount, status))
            
            order_id = cursor.fetchone()[0]
            conn.commit()
            
        print(f"✅ Order {order_id} created for user {student_phone}")
        return order_id
    except Exception as e:
        print(f"❌ Error creating order: {e}")
        return None
    finally:
        if conn: conn.close()

def get_order_details(order_id):
    """Get order details by ID."""
    try:
        conn = create_connection()
        if not conn: return None

        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
            order = cursor.fetchone()
        return dict(order) if order else None
    except Exception as e:
        print(f"❌ Error getting order details for {order_id}: {e}")
        return None
    finally:
        if conn: conn.close()

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

def update_order_status(order_id, status):
    """Update order status."""
    try:
        conn = create_connection()
        if not conn: return False

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
        if conn: conn.close()

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

def set_session_state(student_phone, state, order_id=None):
    """Set user session state (Upsert)."""
    try:
        conn = create_connection()
        if not conn: return False
        
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
        if conn: conn.close()

def get_session_state(student_phone):
    """Get user session state."""
    try:
        conn = create_connection()
        if not conn: return 'initial'

        student_phone = str(student_phone)
        with conn.cursor() as cursor:
            cursor.execute('SELECT state FROM user_sessions WHERE student_phone = %s', (student_phone,))
            result = cursor.fetchone()
        return result[0] if result else 'initial'
    except Exception as e:
        print(f"❌ Error getting session state: {e}")
        return 'initial'
    finally:
        if conn: conn.close()

def get_session_order_id(student_phone):
    """Get current order ID from session."""
    try:
        conn = create_connection()
        if not conn: return None
        
        student_phone = str(student_phone)

        with conn.cursor() as cursor:
            cursor.execute('SELECT current_order_id FROM user_sessions WHERE student_phone = %s', (student_phone,))
            result = cursor.fetchone()
        return result[0] if result else None
    except Exception as e:
        print(f"❌ Error getting session order ID: {e}")
        return None
    finally:
        if conn: conn.close()

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
