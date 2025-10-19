"""
NOX Crime Bot - Fully Automated with Stripe + Webhooks

USER FLOW:
1. User: /start
2. Bot: Shows welcome + /subscribe button
3. User: /subscribe
4. Bot: Sends Stripe payment link
5. User: Pays on Stripe checkout page
6. Stripe: Sends webhook to bot
7. Bot: Auto-activates user instantly
8. User: Receives welcome brief immediately

ZERO MANUAL ACTIVATION NEEDED
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import requests
from collections import Counter
import sqlite3
import stripe
from threading import Thread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from flask import Flask, request, jsonify
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

# Flask app for webhooks
webhook_app = Flask(__name__)


class UserDatabase:
    """User management with Stripe integration"""
    
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
                work_address TEXT,
                work_lat REAL,
                work_lon REAL,
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
        """Store checkout session to link payment to user"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO pending_checkouts (checkout_session_id, user_id, tier)
            VALUES (?, ?, ?)
        ''', (session_id, user_id, tier))
        conn.commit()
        conn.close()
    
    def get_user_from_checkout(self, session_id: str) -> Optional[tuple]:
        """Get user_id and tier from checkout session"""
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
        """Activate user subscription from webhook"""
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
    
    def set_address(self, user_id: int, address_type: str, address: str, lat: float, lon: float):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if address_type == 'home':
            cursor.execute('''
                UPDATE users SET home_address = ?, home_lat = ?, home_lon = ?
                WHERE user_id = ?
            ''', (address, lat, lon, user_id))
        else:
            cursor.execute('''
                UPDATE users SET work_address = ?, work_lat = ?, work_lon = ?
                WHERE user_id = ?
            ''', (address, lat, lon, user_id))
        
        conn.commit()
        conn.close()
    
    def get_user_addresses(self, user_id: int) -> Dict:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT home_address, home_lat, home_lon, work_address, work_lat, work_lon
            FROM users WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            return {}
        
        return {
            'home': {'address': result[0], 'lat': result[1], 'lon': result[2]} if result[0] else None,
            'work': {'address': result[3], 'lat': result[4], 'lon': result[5]} if result[3] else None
        }
    
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


class ChicagoCrimeAnalyzer:
    """Crime analysis with geo-filtering"""
    
    API_URL = "https://data.cityofchicago.org/resource/ijzp-q8t2.json"
    
    def __init__(self):
        self.geocoder = Nominatim(user_agent="nox_crime_bot")
    
    def geocode_address(self, address: str) -> Optional[tuple]:
        try:
            location = self.geocoder.geocode(f"{address}, Chicago, IL")
            if location:
                return (location.latitude, location.longitude)
        except Exception as e:
            logger.error(f"Geocoding failed: {e}")
        return None
    
    def fetch_crimes_near_location(self, lat: float, lon: float, radius_km: float = 0.5) -> List[Dict]:
        end_date = datetime.now() - timedelta(days=3)
        start_date = end_date - timedelta(days=7)
        
        params = {
            "$where": f"date between '{start_date.isoformat()}' and '{end_date.isoformat()}'",
            "$limit": 5000
        }
        
        try:
            response = requests.get(self.API_URL, params=params, timeout=30)
            response.raise_for_status()
            all_crimes = response.json()
            
            nearby_crimes = []
            for crime in all_crimes:
                if 'latitude' in crime and 'longitude' in crime:
                    crime_loc = (float(crime['latitude']), float(crime['longitude']))
                    user_loc = (lat, lon)
                    distance = geodesic(user_loc, crime_loc).kilometers
                    
                    if distance <= radius_km:
                        crime['distance_km'] = distance
                        nearby_crimes.append(crime)
            
            return nearby_crimes
        except Exception as e:
            logger.error(f"API error: {e}")
            return []
    
    def generate_personalized_brief(self, addresses: Dict) -> str:
        if not addresses.get('home'):
            return "⚠️ Set your home address first: /setaddress home 123 Main St Chicago"
        
        home = addresses['home']
        home_crimes = self.fetch_crimes_near_location(home['lat'], home['lon'])
        
        if not home_crimes:
            return "⚠️ No crime data available for your area."
        
        crime_types = Counter(c.get('primary_type') for c in home_crimes)
        top_crimes = crime_types.most_common(3)
        
        brief = f"""🌑 NOX CRIME INTELLIGENCE
Your Weekly Personal Brief
━━━━━━━━━━━━━━━━━━━━━━

📍 YOUR HOME (0.5 mi radius)
{len(home_crimes)} incidents this week

⚠️ TOP THREATS:
"""
        
        for i, (crime_type, count) in enumerate(top_crimes, 1):
            pct = (count / len(home_crimes) * 100)
            brief += f"{i}. {crime_type.title()}: {count} ({pct:.0f}%)\n"
        
        if addresses.get('work'):
            work = addresses['work']
            work_crimes = self.fetch_crimes_near_location(work['lat'], work['lon'])
            brief += f"\n📍 YOUR COMMUTE: {len(work_crimes)} incidents along route\n"
        
        brief += f"""
━━━━━━━━━━━━━━━━━━━━━━
Personalized for YOUR addresses.
/share - Warn your neighbors
━━━━━━━━━━━━━━━━━━━━━━
"""
        
        return brief


class NOXBotHandler:
    """Bot handlers with Stripe integration"""
    
    PRICING = {
        'personal': {'name': 'Personal', 'price': '$1.99/week'},
        'family': {'name': 'Family', 'price': '$3.99/week'},
        'premium': {'name': 'Premium', 'price': '$9.99/week'}
    }
    
    def __init__(self, analyzer: ChicagoCrimeAnalyzer, user_db: UserDatabase, bot_app):
        self.analyzer = analyzer
        self.user_db = user_db
        self.bot_app = bot_app
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        self.user_db.register_user(user.id, user.username, user.first_name)
        
        msg = f"""🌑 NOX CRIME INTELLIGENCE

Personalized crime intelligence for YOUR neighborhood.

Unlike generic city stats, NOX tracks crime specifically near YOUR home and commute.

Try it:
/setaddress home 123 Main St Chicago
/crime - See your personalized brief
/subscribe - Get weekly updates

━━━━━━━━━━━━━━━━━━━━━━
"Your line to what's happening."
"""
        
        await update.message.reply_text(msg)
    
    async def setaddress_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage:\n"
                "/setaddress home 123 Main St Chicago\n"
                "/setaddress work 456 State St Chicago"
            )
            return
        
        address_type = context.args[0].lower()
        address = ' '.join(context.args[1:])
        
        if address_type not in ['home', 'work']:
            await update.message.reply_text("Type must be 'home' or 'work'")
            return
        
        await update.message.reply_text("⏳ Geocoding...")
        
        coords = self.analyzer.geocode_address(address)
        if not coords:
            await update.message.reply_text("❌ Address not found. Try: '123 Main St, Chicago'")
            return
        
        self.user_db.set_address(update.effective_user.id, address_type, address, coords[0], coords[1])
        
        await update.message.reply_text(
            f"✅ {address_type.title()} address set!\n\n"
            f"/crime - See personalized brief\n"
            f"/subscribe - Get weekly updates"
        )
    
    async def subscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show subscription tiers"""
        keyboard = [
            [InlineKeyboardButton("Personal - $1.99/week", callback_data='sub_personal')],
            [InlineKeyboardButton("Family - $3.99/week", callback_data='sub_family')],
            [InlineKeyboardButton("Premium - $9.99/week", callback_data='sub_premium')]
        ]
        
        await update.message.reply_text(
            "🌑 Choose your plan:\n\n"
            "**Personal** ($1.99/week)\n"
            "• 1 home address\n"
            "• Weekly briefs\n\n"
            "**Family** ($3.99/week)\n"
            "• 3 addresses\n"
            "• Protect family\n\n"
            "**Premium** ($9.99/week)\n"
            "• Daily updates\n"
            "• SMS alerts\n",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    async def subscription_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tier selection and create Stripe checkout"""
        query = update.callback_query
        await query.answer()
        
        tier = query.data.replace('sub_', '')
        user = update.effective_user
        
        # Get correct Stripe Price ID from env
        price_id = os.getenv(f'STRIPE_PRICE_{tier.upper()}')
        
        if not price_id:
            await query.message.reply_text("❌ Configuration error. Contact admin.")
            return
        
        try:
            # Create Stripe Checkout Session
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price': price_id,
                    'quantity': 1,
                }],
                mode='subscription',
                success_url='https://t.me/' + context.bot.username + '?start=success',
                cancel_url='https://t.me/' + context.bot.username + '?start=cancel',
                client_reference_id=str(user.id),
                metadata={
                    'user_id': user.id,
                    'username': user.username or '',
                    'tier': tier
                }
            )
            
            # Save checkout session
            self.user_db.save_checkout_session(checkout_session.id, user.id, tier)
            
            # Send payment link
            await query.message.reply_text(
                f"💳 Complete your subscription:\n\n"
                f"{checkout_session.url}\n\n"
                f"After payment, you'll be instantly activated!"
            )
            
        except Exception as e:
            logger.error(f"Stripe checkout error: {e}")
            await query.message.reply_text("❌ Payment error. Try again or contact admin.")
    
    async def crime_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        
        addresses = self.user_db.get_user_addresses(user.id)
        
        if not addresses.get('home'):
            await update.message.reply_text(
                "⚠️ Set your address first:\n"
                "/setaddress home 123 Main St Chicago"
            )
            return
        
        await update.message.reply_text("⏳ Analyzing crime near you...")
        
        brief = self.analyzer.generate_personalized_brief(addresses)
        await update.message.reply_text(brief)
        
        # Prompt to subscribe if not active
        if not self.user_db.is_active(user.id):
            keyboard = [[InlineKeyboardButton("Subscribe for Weekly Briefs", callback_data='sub_personal')]]
            await update.message.reply_text(
                "👆 Want this every Monday?\n/subscribe",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Prompt to share
            keyboard = [[InlineKeyboardButton("⚠️ Warn Neighbors", callback_data='share_warning')]]
            await update.message.reply_text(
                "Share with neighbors?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    async def share_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        share_msg = f"""⚠️ CRIME ALERT

Crime activity in our neighborhood this week.

Get personalized intelligence: @{context.bot.username}

Free trial: /start

━━━━━━━━━━━━━━━━━━━━━━
"Your line to what's happening."
"""
        
        await query.message.reply_text(
            share_msg + "\n\n👆 Copy and share in group chats"
        )
    
    async def send_activation_message(self, user_id: int):
        """Send welcome message after payment"""
        try:
            addresses = self.user_db.get_user_addresses(user_id)
            
            if addresses.get('home'):
                # Generate first brief
                brief = self.analyzer.generate_personalized_brief(addresses)
                await self.bot_app.bot.send_message(
                    chat_id=user_id,
                    text=f"✅ SUBSCRIPTION ACTIVE!\n\nHere's your first brief:\n\n{brief}"
                )
            else:
                await self.bot_app.bot.send_message(
                    chat_id=user_id,
                    text="✅ SUBSCRIPTION ACTIVE!\n\n"
                         "Set your address to get personalized briefs:\n"
                         "/setaddress home 123 Main St Chicago"
                )
        except Exception as e:
            logger.error(f"Failed to send activation message to {user_id}: {e}")


# Global references for webhook
user_db = None
bot_handler = None


@webhook_app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhooks"""
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400
    
    # Handle checkout.session.completed
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        # Get user info
        user_id = int(session['metadata']['user_id'])
        tier = session['metadata']['tier']
        customer_id = session['customer']
        subscription_id = session['subscription']
        email = session.get('customer_details', {}).get('email')
        
        # Activate subscription
        user_db.activate_subscription(user_id, customer_id, subscription_id, tier, email)
        
        # Send activation message (async)
        import asyncio
        asyncio.run(bot_handler.send_activation_message(user_id))
        
        logger.info(f"Webhook: Activated user {user_id}, tier {tier}")
    
    return jsonify({'status': 'success'}), 200


def run_webhook_server():
    """Run Flask webhook server"""
    webhook_app.run(host='0.0.0.0', port=5000)


def main():
    global user_db, bot_handler
    
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    ADMIN_USER_ID = os.getenv('ADMIN_USER_ID')
    
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")
    
    # Initialize
    user_db = UserDatabase()
    analyzer = ChicagoCrimeAnalyzer()
    application = Application.builder().token(TOKEN).build()
    bot_handler = NOXBotHandler(analyzer, user_db, application)
    
    if ADMIN_USER_ID:
        user_db.add_admin(int(ADMIN_USER_ID))
    
    # Register handlers
    application.add_handler(CommandHandler("start", bot_handler.start_command))
    application.add_handler(CommandHandler("setaddress", bot_handler.setaddress_command))
    application.add_handler(CommandHandler("subscribe", bot_handler.subscribe_command))
    application.add_handler(CommandHandler("crime", bot_handler.crime_command))
    
    application.add_handler(CallbackQueryHandler(bot_handler.subscription_callback, pattern='^sub_'))
    application.add_handler(CallbackQueryHandler(bot_handler.share_callback, pattern='^share_'))
    
    # Start webhook server in background thread
    webhook_thread = Thread(target=run_webhook_server, daemon=True)
    webhook_thread.start()
    
    logger.info("🌑 NOX Crime Bot - Fully Automated")
    logger.info("💳 Stripe webhooks ready on port 5000")
    
    # Run bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()