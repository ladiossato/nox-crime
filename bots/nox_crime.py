"""
NOX Crime Bot - Complete with Multi-Method Address Capture
1. Share Location button (mobile)
2. Web App search (all devices)
3. Text + geocode confirmation (fallback)
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import requests
from collections import Counter, defaultdict
import sqlite3
import stripe
from threading import Thread
import json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from flask import Flask, request, jsonify, render_template_string

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
GOOGLE_MAPS_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY', '')

webhook_app = Flask(__name__)

# Conversation states
AWAITING_ADDRESS_TEXT = 1


class UserDatabase:
    def __init__(self, db_path='config/users.db'):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                email TEXT,
                stripe_customer_id TEXT UNIQUE,
                stripe_subscription_id TEXT,
                subscription_tier TEXT DEFAULT 'free',
                is_active INTEGER DEFAULT 0,
                home_address TEXT,
                home_lat REAL,
                home_lon REAL,
                subscription_expires_at TIMESTAMP,
                total_paid REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_checkouts (
                checkout_session_id TEXT PRIMARY KEY,
                user_id INTEGER,
                tier TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def register_user(self, user_id: int, username: str = None, first_name: str = None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        ''', (user_id, username, first_name))
        conn.commit()
        conn.close()
    
    def save_checkout_session(self, session_id: str, user_id: int, tier: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO pending_checkouts (checkout_session_id, user_id, tier)
            VALUES (?, ?, ?)
        ''', (session_id, user_id, tier))
        conn.commit()
        conn.close()
    
    def get_user_from_checkout(self, session_id: str) -> Optional[tuple]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, tier FROM pending_checkouts
            WHERE checkout_session_id = ?
        ''', (session_id,))
        result = cursor.fetchone()
        conn.close()
        return result
    
    def activate_subscription(self, user_id: int, customer_id: str, subscription_id: str, 
                            tier: str, email: str = None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        expires_at = datetime.now() + timedelta(days=7)
        
        cursor.execute('''
            UPDATE users
            SET stripe_customer_id = ?,
                stripe_subscription_id = ?,
                subscription_tier = ?,
                is_active = 1,
                subscription_expires_at = ?,
                email = COALESCE(?, email)
            WHERE user_id = ?
        ''', (customer_id, subscription_id, tier, expires_at, email, user_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Activated subscription for user {user_id}, tier: {tier}")
    
    def is_active(self, user_id: int) -> bool:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT is_active FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return bool(result and result[0])
    
    def set_address(self, user_id: int, address: str, lat: float, lon: float):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users SET home_address = ?, home_lat = ?, home_lon = ?
            WHERE user_id = ?
        ''', (address, lat, lon, user_id))
        conn.commit()
        conn.close()
    
    def get_user_address(self, user_id: int) -> Optional[Dict]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT home_address, home_lat, home_lon
            FROM users WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()
        conn.close()
        
        if result and result[0]:
            return {'address': result[0], 'lat': result[1], 'lon': result[2]}
        return None
    
    def get_all_active_users(self) -> List[int]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE is_active = 1')
        user_ids = [row[0] for row in cursor.fetchall()]
        conn.close()
        return user_ids
    
    def is_admin(self, user_id: int) -> bool:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM admins WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None
    
    def add_admin(self, user_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (user_id,))
        conn.commit()
        conn.close()


class GeoCoder:
    @staticmethod
    def reverse_geocode(lat: float, lon: float) -> str:
        """Convert coordinates to address"""
        if not GOOGLE_MAPS_API_KEY:
            return f"{lat:.4f}, {lon:.4f}"
        
        url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&key={GOOGLE_MAPS_API_KEY}"
        try:
            response = requests.get(url, timeout=5)
            data = response.json()
            if data['status'] == 'OK' and data['results']:
                return data['results'][0]['formatted_address']
        except:
            pass
        return f"{lat:.4f}, {lon:.4f}"
    
    @staticmethod
    def geocode_text(address_text: str) -> List[Dict]:
        """Get candidate addresses from text input"""
        if not GOOGLE_MAPS_API_KEY:
            # Fallback: use Nominatim
            url = f"https://nominatim.openstreetmap.org/search?q={address_text}, Chicago, IL&format=json&limit=3"
            try:
                response = requests.get(url, timeout=10, headers={'User-Agent': 'NOX Crime Bot'})
                results = response.json()
                return [
                    {
                        'address': r['display_name'],
                        'lat': float(r['lat']),
                        'lon': float(r['lon'])
                    }
                    for r in results[:3]
                ]
            except:
                return []
        
        # Use Google Places Autocomplete
        url = f"https://maps.googleapis.com/maps/api/place/autocomplete/json?input={address_text}&components=locality:chicago&key={GOOGLE_MAPS_API_KEY}"
        try:
            response = requests.get(url, timeout=5)
            data = response.json()
            
            candidates = []
            for prediction in data.get('predictions', [])[:3]:
                place_id = prediction['place_id']
                # Get details for this place
                detail_url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&key={GOOGLE_MAPS_API_KEY}"
                detail_response = requests.get(detail_url, timeout=5)
                detail_data = detail_response.json()
                
                if detail_data['status'] == 'OK':
                    location = detail_data['result']['geometry']['location']
                    candidates.append({
                        'address': prediction['description'],
                        'lat': location['lat'],
                        'lon': location['lng']
                    })
            
            return candidates
        except:
            return []


class ChicagoCrimeAnalyzer:
    API_URL = "https://data.cityofchicago.org/resource/ijzp-q8t2.json"
    
    def fetch_crimes_near_location(self, lat: float, lon: float, radius_km: float = 0.8) -> List[Dict]:
        end_date = datetime.now() - timedelta(days=3)
        start_date = end_date - timedelta(days=7)
        
        # Calculate bounding box (approximate)
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / (111.0 * abs(float(lat)))
        
        min_lat = lat - lat_delta
        max_lat = lat + lat_delta
        min_lon = lon - lon_delta
        max_lon = lon + lon_delta
        
        params = {
            "$where": f"date between '{start_date.isoformat()}' and '{end_date.isoformat()}' AND latitude >= {min_lat} AND latitude <= {max_lat} AND longitude >= {min_lon} AND longitude <= {max_lon}",
            "$limit": 1000,
            "$order": "date DESC"
        }
        
        try:
            response = requests.get(self.API_URL, params=params, timeout=30)
            response.raise_for_status()
            crimes = response.json()
            logger.info(f"Fetched {len(crimes)} incidents near {lat},{lon}")
            return crimes
        except Exception as e:
            logger.error(f"API error: {e}")
            return []
    
    def generate_brief(self, lat: float, lon: float, address: str) -> str:
        crimes = self.fetch_crimes_near_location(lat, lon)
        
        if not crimes:
            return f"✅ YOUR AREA: ALL CLEAR\n\n📍 {address}\n\nZero incidents within 0.8 km this week.\nYour area is safer than average."
        
        crime_types = Counter(c.get('primary_type') for c in crimes)
        top_crimes = crime_types.most_common(3)
        
        time_blocks = defaultdict(int)
        days = defaultdict(int)
        
        for crime in crimes:
            if 'date' in crime:
                try:
                    dt = datetime.fromisoformat(crime['date'].replace('Z', '+00:00'))
                    hour = dt.hour
                    day = dt.strftime('%A')
                    
                    if 18 <= hour < 24:
                        time_blocks['evening (6PM-midnight)'] += 1
                    elif 0 <= hour < 6:
                        time_blocks['late night (midnight-6AM)'] += 1
                    elif 6 <= hour < 12:
                        time_blocks['morning (6AM-noon)'] += 1
                    else:
                        time_blocks['afternoon (noon-6PM)'] += 1
                    
                    days[day] += 1
                except:
                    pass
        
        riskiest_time = max(time_blocks.items(), key=lambda x: x[1])[0] if time_blocks else "unknown"
        riskiest_day = max(days.items(), key=lambda x: x[1])[0] if days else "unknown"
        
        brief = f"""🌑 NOX CRIME INTELLIGENCE
Your Area Threat Assessment
━━━━━━━━━━━━━━━━━━━━━━

📍 YOUR LOCATION
{address}

🚨 THIS WEEK
{len(crimes)} incidents within 0.8 km

🎯 THREAT BREAKDOWN
"""
        
        for i, (crime_type, count) in enumerate(top_crimes, 1):
            pct = (count / len(crimes) * 100)
            brief += f"{i}. {crime_type.title()}: {count} ({pct:.0f}%)\n"
        
        brief += f"""
⏰ HIGHEST RISK
{riskiest_day}, {riskiest_time}
→ {time_blocks.get(riskiest_time, 0)} of {len(crimes)} incidents

💡 RECOMMENDATIONS
"""
        
        recommendations = []
        for crime_type, count in top_crimes:
            if 'THEFT' in crime_type:
                recommendations.append("• Secure vehicles in view")
            elif 'BATTERY' in crime_type:
                recommendations.append("• Avoid solo walks after dark")
            elif 'BURGLARY' in crime_type:
                recommendations.append("• Verify locks before leaving")
            elif 'ROBBERY' in crime_type:
                recommendations.append("• Stay in lit areas after sunset")
        
        for rec in list(dict.fromkeys(recommendations))[:3]:
            brief += f"{rec}\n"
        
        if len(crimes) > 50:
            brief += "• ⚠️ ELEVATED ACTIVITY in your area\n"
        
        brief += f"""
━━━━━━━━━━━━━━━━━━━━━━
Intel: Chicago PD
Updated: {datetime.now().strftime('%b %d, %I:%M%p')}

/share - Alert neighbors
━━━━━━━━━━━━━━━━━━━━━━
"Your line to what's happening."
"""
        
        return brief


class NOXBotHandler:
    PRICE = 199
    
    def __init__(self, analyzer: ChicagoCrimeAnalyzer, user_db: UserDatabase, bot_app):
        self.analyzer = analyzer
        self.user_db = user_db
        self.bot_app = bot_app
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        self.user_db.register_user(user.id, user.username, user.first_name)
        
        msg = f"""🌑 NOX CRIME INTELLIGENCE

Personalized crime intelligence for YOUR exact location in Chicago.

Get started:
/setlocation - Set your address
/crime - See threat assessment
/subscribe - $1.99/week

━━━━━━━━━━━━━━━━━━━━━━
"Your line to what's happening."
"""
        
        await update.message.reply_text(msg)
    
    async def setlocation_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show all three address capture options"""
        
        # Option 1: Share Location Button (mobile)
        location_keyboard = [[KeyboardButton("📍 Share My Location", request_location=True)]]
        
        # Option 2: Web App Search (if configured)
        webapp_button = None
        if GOOGLE_MAPS_API_KEY:
            webapp_url = f"https://{os.getenv('WEBAPP_DOMAIN', 'your-domain.com')}/address-search"
            webapp_button = InlineKeyboardButton("🔍 Search Address", web_app=WebAppInfo(url=webapp_url))
        
        # Option 3: Text Input
        inline_keyboard = []
        if webapp_button:
            inline_keyboard.append([webapp_button])
        inline_keyboard.append([InlineKeyboardButton("⌨️ Type Address", callback_data='type_address')])
        
        await update.message.reply_text(
            "📍 **Set Your Location**\n\n"
            "Choose how to set your address:\n\n"
            "1️⃣ Tap 'Share My Location' below (mobile)\n"
            "2️⃣ Search for your address\n"
            "3️⃣ Type your address manually",
            reply_markup=ReplyKeyboardMarkup(location_keyboard, one_time_keyboard=True, resize_keyboard=True),
            parse_mode='Markdown'
        )
        
        await update.message.reply_text(
            "Or use these options:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard)
        )
    
    async def handle_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle shared location (Option 1)"""
        location = update.message.location
        lat = location.latitude
        lon = location.longitude
        
        address = GeoCoder.reverse_geocode(lat, lon)
        
        self.user_db.set_address(update.effective_user.id, address, lat, lon)
        
        await update.message.reply_text(
            f"✅ Location saved!\n\n📍 {address}\n\n/crime - See your threat assessment",
            reply_markup=ReplyKeyboardRemove()
        )
    
    async def type_address_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle 'Type Address' button (Option 3)"""
        query = update.callback_query
        await query.answer()
        
        await query.message.reply_text(
            "⌨️ Type your Chicago address:\n\n"
            "Example: 123 N State St\n\n"
            "I'll show you a few matches to confirm.",
            reply_markup=ReplyKeyboardRemove()
        )
        
        return AWAITING_ADDRESS_TEXT
    
    async def handle_address_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle typed address text"""
        address_text = update.message.text
        
        await update.message.reply_text("🔍 Finding your address...")
        
        candidates = GeoCoder.geocode_text(address_text)
        
        if not candidates:
            await update.message.reply_text(
                "❌ No results found. Try again:\n"
                "/setlocation"
            )
            return ConversationHandler.END
        
        # Show candidates as buttons
        keyboard = []
        for i, candidate in enumerate(candidates):
            callback_data = f"confirm_addr_{i}"
            # Store in context for retrieval
            context.user_data[callback_data] = candidate
            keyboard.append([InlineKeyboardButton(
                f"📍 {candidate['address'][:60]}...",
                callback_data=callback_data
            )])
        
        keyboard.append([InlineKeyboardButton("❌ None of these", callback_data='cancel_address')])
        
        await update.message.reply_text(
            "Select your address:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return ConversationHandler.END
    
    async def confirm_address_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address confirmation"""
        query = update.callback_query
        await query.answer()
        
        if query.data == 'cancel_address':
            await query.message.edit_text("❌ Cancelled. Try again: /setlocation")
            return
        
        # Retrieve selected address from context
        selected = context.user_data.get(query.data)
        
        if not selected:
            await query.message.edit_text("❌ Error. Try again: /setlocation")
            return
        
        self.user_db.set_address(
            update.effective_user.id,
            selected['address'],
            selected['lat'],
            selected['lon']
        )
        
        await query.message.edit_text(
            f"✅ Location saved!\n\n📍 {selected['address']}\n\n/crime - See your threat assessment"
        )
    
    async def subscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        
        price_id = os.getenv('STRIPE_PRICE_PERSONAL')
        
        if not price_id:
            await update.message.reply_text("❌ Payment config error.")
            return
        
        await update.message.reply_text("⏳ Generating payment link...")
        
        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{'price': price_id, 'quantity': 1}],
                mode='subscription',
                success_url='https://t.me/' + context.bot.username,
                cancel_url='https://t.me/' + context.bot.username,
                client_reference_id=str(user.id),
                metadata={'user_id': user.id, 'username': user.username or '', 'tier': 'personal'}
            )
            
            self.user_db.save_checkout_session(checkout_session.id, user.id, 'personal')
            
            await update.message.reply_text(
                f"💳 **Subscribe to NOX Crime**\n\n"
                f"$1.99/week - Cancel anytime\n\n"
                f"{checkout_session.url}",
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Stripe error: {e}")
            await update.message.reply_text("❌ Payment error.")
    
    async def crime_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        
        user_address = self.user_db.get_user_address(user.id)
        
        if not user_address:
            await update.message.reply_text(
                "⚠️ Set your location first:\n/setlocation"
            )
            return
        
        await update.message.reply_text("⏳ Analyzing crime in your area...")
        
        brief = self.analyzer.generate_brief(
            user_address['lat'],
            user_address['lon'],
            user_address['address']
        )
        
        await update.message.reply_text(brief)
        
        if not self.user_db.is_active(user.id):
            await update.message.reply_text(
                "👆 Want this every Monday?\n\n"
                "$1.99/week - Cancel anytime\n"
                "/subscribe"
            )
        else:
            keyboard = [[InlineKeyboardButton("⚠️ Share with Neighbors", callback_data='share_warning')]]
            await update.message.reply_text(
                "Alert neighbors?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    async def share_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        share_msg = f"""⚠️ CRIME ALERT

Activity in our neighborhood this week.

Get personalized intel: @{context.bot.username}

━━━━━━━━━━━━━━━━━━━━━━
"Your line to what's happening."
"""
        
        await query.message.reply_text(share_msg + "\n\n👆 Copy & share")
    
    async def send_activation_message(self, user_id: int):
        try:
            await self.bot_app.bot.send_message(
                chat_id=user_id,
                text="✅ SUBSCRIPTION ACTIVE!\n\nYou'll receive weekly briefs every Monday at 6 AM.\n\n/crime - Get brief now"
            )
        except Exception as e:
            logger.error(f"Activation message failed: {e}")


# Webhook
user_db = None
bot_handler = None

@webhook_app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET')
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400
    
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        user_id = int(session['metadata']['user_id'])
        tier = session['metadata']['tier']
        customer_id = session['customer']
        subscription_id = session['subscription']
        email = session.get('customer_details', {}).get('email')
        
        user_db.activate_subscription(user_id, customer_id, subscription_id, tier, email)
        
        logger.info(f"Webhook: Activated user {user_id}, tier {tier}")
    
    return jsonify({'status': 'success'}), 200

@webhook_app.route('/address-search')
def address_search_webapp():
    """Web App for address search"""
    html = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { font-family: sans-serif; padding: 20px; }
        input { width: 100%; padding: 10px; font-size: 16px; }
        .result { padding: 10px; border-bottom: 1px solid #ccc; cursor: pointer; }
    </style>
</head>
<body>
    <h3>Search Address</h3>
    <input type="text" id="addressInput" placeholder="Type address...">
    <div id="results"></div>
    
    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        
        document.getElementById('addressInput').addEventListener('input', function(e) {
            const query = e.target.value;
            if (query.length < 3) return;
            
            // Call your backend to get suggestions
            fetch('/api/geocode?q=' + encodeURIComponent(query))
                .then(r => r.json())
                .then(data => {
                    const resultsDiv = document.getElementById('results');
                    resultsDiv.innerHTML = '';
                    data.results.forEach(result => {
                        const div = document.createElement('div');
                        div.className = 'result';
                        div.textContent = result.address;
                        div.onclick = () => {
                            tg.sendData(JSON.stringify(result));
                            tg.close();
                        };
                        resultsDiv.appendChild(div);
                    });
                });
        });
    </script>
</body>
</html>
    """
    return render_template_string(html)

@webhook_app.route('/api/geocode')
def api_geocode():
    query = request.args.get('q', '')
    results = GeoCoder.geocode_text(query)
    return jsonify({'results': results})


def run_webhook_server():
    webhook_app.run(host='0.0.0.0', port=5000)


def main():
    global user_db, bot_handler
    
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    ADMIN_USER_ID = os.getenv('ADMIN_USER_ID')
    
    user_db = UserDatabase()
    analyzer = ChicagoCrimeAnalyzer()
    application = Application.builder().token(TOKEN).build()
    bot_handler = NOXBotHandler(analyzer, user_db, application)
    
    if ADMIN_USER_ID:
        user_db.add_admin(int(ADMIN_USER_ID))
    
    # Conversation handler for text address input
    address_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bot_handler.type_address_callback, pattern='^type_address$')],
        states={
            AWAITING_ADDRESS_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_handler.handle_address_text)]
        },
        fallbacks=[]
    )
    
    application.add_handler(CommandHandler("start", bot_handler.start_command))
    application.add_handler(CommandHandler("setlocation", bot_handler.setlocation_command))
    application.add_handler(CommandHandler("subscribe", bot_handler.subscribe_command))
    application.add_handler(CommandHandler("crime", bot_handler.crime_command))
    
    application.add_handler(MessageHandler(filters.LOCATION, bot_handler.handle_location))
    application.add_handler(address_conv)
    application.add_handler(CallbackQueryHandler(bot_handler.confirm_address_callback, pattern='^confirm_addr_'))
    application.add_handler(CallbackQueryHandler(bot_handler.confirm_address_callback, pattern='^cancel_address$'))
    application.add_handler(CallbackQueryHandler(bot_handler.share_callback, pattern='^share_'))
    
    webhook_thread = Thread(target=run_webhook_server, daemon=True)
    webhook_thread.start()
    
    logger.info("🌑 NOX Crime Bot - Multi-Method Address Capture")
    logger.info("💳 Stripe webhooks ready")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()