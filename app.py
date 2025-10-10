# ... (around line 1756)

# Handler for all text messages (used for admin commands, phone entry, and quantity entry)
@bot.message_handler(content_types=['text'])
def handle_text_messages(message):
    chat_id = message.chat.id
    student_db_id = str(chat_id)
    text = message.text.strip()
    
    is_admin = chat_id in ADMIN_CHAT_IDS

    # --- NEW: Catch simple 'menu' text and redirect to the button handler ---
    # This ensures a non-admin user typing "menu" is subject to the normal logic/time checks.
    if text.lower() == 'menu':
        # Create a mock message object with the full button text to satisfy the handler logic
        mock_message = type('MockMessage', (object,), {'text': 'Menu 🍽️', 'chat': message.chat})()
        return handle_reply_keyboard_buttons(mock_message)

    # 1. ADMIN COMMAND HANDLING (Highest Priority, No Time Limit)
    if is_admin:
        # If admin sends an unknown text command, handle it as a potential menu command.
        if text.lower() not in ['menu 🍽️', 'order status 📊', 'admin panel ⚙️', 'orders 📦']:
            handle_admin_text_commands(text, chat_id)
            return
        
    # 2. TIME CHECK for ALL subsequent customer actions (includes phone/quantity text inputs)
    if not is_admin and not is_bot_available_now():
        unavailable_message(chat_id)
        return
    
    current_state = db_manager.get_session_state(student_db_id)
    current_order_id = db_manager.get_session_order_id(student_db_id)

    # 3. PHONE NUMBER INPUT (Awaiting Phone Number State)
    # ... (rest of the function, which remains unchanged)
    if current_state == 'awaiting_phone_number':
        # Simple validation: ensure it contains only digits/+, and at least 7 digits
        phone_match = re.match(r'^[+\d]{7,}$', text)
        if not phone_match:
            bot.send_message(chat_id, "❌ Invalid phone number format. Please enter a valid number (e.g., `+919876543210` or just `9876543210`).")
            return

        # Save the phone number
        db_manager.update_user_phone(student_db_id, text)
        
        # Resume the checkout flow 
        order_details = db_manager.get_order_details(current_order_id)
        service_type = order_details.get('service_type', 'parcel') # Use saved service type
        
        # Send a prompt to the user with the confirmation inline keyboard
        items_list = db_manager.parse_order_items(order_details['items'])
        food_summary = "\n".join([
            f"• {item['name'].title()} x {item['qty']} (₹{item['price']:.2f})"
            for item in items_list
        ])
        
        contact_display = db_manager.get_user_phone(student_db_id)

        confirmation_msg = (
            f"📝 *Final Order Confirmation (ID: #{current_order_id}):*\n\n"
            f"📞 **Contact:** `{contact_display}`\n"
            f"🪑 **Service Type:** {service_type.replace('_', ' ').title()}\n"
            f"💰 **Total Amount:** ₹{order_details['total_amount']:.2f}\n\n"
            f"🍽️ *Items:*\n{food_summary}\n\n"
            f"✅ Contact saved. Press **'✅ Confirm & Pay'** to proceed to Razorpay."
        )
        
        db_manager.set_session_state(student_db_id, 'confirming_order', current_order_id)

        # Remove the Reply Keyboard before sending the new inline keyboard message
        bot.send_message(chat_id, "✅ Contact received!", reply_markup=ReplyKeyboardRemove())
        
        bot.send_message(chat_id, confirmation_msg, parse_mode='Markdown', 
                         reply_markup=get_confirmation_inline_keyboard())
        return

    # 4. TYPED QUANTITY INPUT (Awaiting Typed Quantity State)
    elif current_state.startswith('awaiting_typed_quantity_'):
        try:
            quantity = int(text)
            if quantity <= 0:
                raise ValueError("Quantity must be positive.")
                
            # Extract item_id from the state string (e.g., 'awaiting_typed_quantity_123')
            item_id = int(current_state.split('_')[-1])
            
            # Send initial message to remove the reply keyboard
            bot.send_message(chat_id, f"✅ Quantity {quantity} received. Processing...", reply_markup=ReplyKeyboardRemove())

            # Add item to cart and prompt for next step (message_id=None as this is a new message)
            add_item_to_cart_and_prompt(student_db_id, chat_id, message_id=None, item_id=item_id, quantity=quantity)
            
        except ValueError:
            bot.send_message(chat_id, "❌ Invalid quantity. Please type a valid whole number greater than 0.")
        return

    # 5. DEFAULT FALLBACK
    else:
        # Default response for unhandled text, ensuring the main keyboard is visible
        main_keyboard = get_admin_reply_keyboard() if is_admin else get_main_reply_keyboard()
        bot.send_message(chat_id, "I'm a Canteen Bot! Please use the buttons below or type /start to begin.", 
                         reply_markup=main_keyboard)
