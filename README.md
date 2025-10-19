# 🌑 NOX Crime Intelligence Bot

**Personalized crime intelligence for Chicago, delivered weekly via Telegram.**

Unlike generic city-wide crime stats, NOX tracks crime specifically within 0.5 miles of YOUR home and commute route. Think Bloomberg Terminal meets neighborhood safety.

---

## 🎯 What It Does

- **Personalized Intelligence**: Crime data specific to your address, not the entire city
- **Weekly Reports**: Automated briefs every Monday at 6 AM Central
- **On-Demand Access**: `/crime` command for instant intelligence
- **Smart Insights**: Week-over-week trends, hotspot analysis, actionable recommendations
- **Automated Payments**: Stripe-powered subscriptions with auto-activation

---

## 💰 Pricing

- **Personal**: $1.99/week - Track 1 home address
- **Family**: $3.99/week - Track up to 3 addresses
- **Premium**: $9.99/week - Daily updates + SMS alerts

---

## 🚀 Tech Stack

- **Bot Framework**: python-telegram-bot (async)
- **Payments**: Stripe Checkout + Webhooks
- **Data Source**: Chicago Police Department Open Data API
- **Geocoding**: Geopy + Nominatim
- **Database**: SQLite (easy migration to Postgres)
- **Scheduling**: APScheduler (cron-style jobs)
- **Hosting**: Render.com / Railway.app (free tier compatible)

---

## 📁 Project Structure

```
nox/
├── config/
│   └── users.db              # SQLite database (auto-generated)
├── bots/
│   └── nox_crime.py          # Main bot application
├── .env                      # Environment variables (NOT in git)
├── .env.example              # Template for setup
├── requirements.txt          # Python dependencies
└── README.md                 # You are here
```

---

## ⚙️ Setup Instructions

### **1. Prerequisites**

- Python 3.11+
- Telegram account
- Stripe account (free)
- Stripe CLI (for local webhook testing)

### **2. Install Dependencies**

```bash
pip install -r requirements.txt
```

### **3. Create Telegram Bot**

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow prompts
3. Save your bot token

### **4. Configure Stripe**

1. Sign up at [stripe.com](https://stripe.com)
2. Get API keys from [dashboard.stripe.com/test/apikeys](https://dashboard.stripe.com/test/apikeys)
3. Create products in Stripe dashboard:
   - Personal: $1.99/week
   - Family: $3.99/week  
   - Premium: $9.99/week
4. Copy Price IDs for each product

### **5. Set Environment Variables**

Create `.env` file in project root:

```bash
# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here

# Stripe (use test keys first)
STRIPE_SECRET_KEY=sk_test_xxxxx
STRIPE_PRICE_PERSONAL=price_xxxxx
STRIPE_PRICE_FAMILY=price_xxxxx
STRIPE_PRICE_PREMIUM=price_xxxxx

# Admin
ADMIN_USER_ID=your_telegram_user_id  # Get from @userinfobot

# Webhook (for production)
STRIPE_WEBHOOK_SECRET=whsec_xxxxx
```

### **6. Test Locally**

**Terminal 1 - Run Stripe webhook listener:**
```bash
stripe listen --forward-to localhost:5000/webhook/stripe
```

Copy the webhook secret (`whsec_xxxxx`) and add to `.env`

**Terminal 2 - Run bot:**
```bash
python bots/nox_crime.py
```

### **7. Test Payment Flow**

1. Message your bot: `/start`
2. Set address: `/setaddress home 123 N State St Chicago`
3. Request brief: `/crime`
4. Subscribe: `/subscribe`
5. Pay with test card: `4242 4242 4242 4242`
6. Verify activation message received

---

## 🚀 Deployment (Production)

### **Option 1: Render.com (Recommended)**

1. Create account at [render.com](https://render.com)
2. New Web Service → Connect GitHub repo
3. Configure:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bots/nox_crime.py`
   - **Environment Variables**: Copy all from `.env`
4. Deploy

### **Option 2: Railway.app**

Similar process - great free tier, automatic deployments from Git.

### **Production Webhook Setup**

1. Deploy to hosting platform
2. Note your app URL: `https://your-app.onrender.com`
3. In Stripe dashboard:
   - Go to Webhooks → Add endpoint
   - URL: `https://your-app.onrender.com/webhook/stripe`
   - Events: `checkout.session.completed`
4. Copy signing secret → Update `STRIPE_WEBHOOK_SECRET` in production

### **Switch to Live Mode**

1. In Stripe dashboard, toggle to "Live mode"
2. Get live API keys and Price IDs
3. Update environment variables in hosting platform
4. Redeploy

---

## 🤖 Bot Commands

### **User Commands:**
- `/start` - Welcome message and setup instructions
- `/setaddress home [address]` - Set home address
- `/setaddress work [address]` - Set work/commute address
- `/crime` - Get on-demand crime intelligence brief
- `/subscribe` - View subscription options and payment
- `/status` - Check subscription status
- `/share` - Generate shareable warning for neighbors
- `/cancel` - Cancel subscription
- `/terms` - View Terms of Service
- `/privacy` - View Privacy Policy

### **Admin Commands:**
- `/stats` - Business metrics (revenue, users, MRR)
- `/users` - List active subscribers
- `/broadcast [message]` - Send to all active users

---

## 📊 Data Sources

- **Chicago Police Department**: [Crime Data Portal](https://data.cityofchicago.org/resource/ijzp-q8t2.json)
- **Update Frequency**: Daily (3-7 day reporting lag from CPD)
- **Coverage**: All Chicago incidents from 2001-present
- **Geocoding**: OpenStreetMap via Nominatim

---

## 🔒 Security & Privacy

- **Data Storage**: User addresses stored locally, never shared
- **Payment Security**: All payment data handled by Stripe (PCI compliant)
- **API Keys**: Stored in environment variables, never committed to Git
- **User Data**: Can be deleted on request via `/delete` command

---

## 🐛 Troubleshooting

### **Bot doesn't respond**
```bash
# Check if bot is running
ps aux | grep python

# Check logs
tail -f logs/bot.log  # If using production logging
```

### **Geocoding fails**
- Increase timeout in `ChicagoCrimeAnalyzer.__init__()`:
  ```python
  self.geocoder = Nominatim(user_agent="nox_crime_bot", timeout=10)
  ```

### **Webhook signature errors**
- Verify `STRIPE_WEBHOOK_SECRET` matches Stripe dashboard
- Check webhook endpoint URL is correct
- Ensure webhook is receiving `checkout.session.completed` events

### **Payment not activating user**
- Check Render/Railway logs for webhook errors
- Verify Price IDs in `.env` match Stripe dashboard
- Test webhook manually via Stripe dashboard → Send test event

---

## 📈 Roadmap

### **v1.0 (Current)**
- [x] Personalized crime intelligence
- [x] Automated weekly reports
- [x] Stripe subscription payments
- [x] Webhook auto-activation

### **v1.1 (Next)**
- [ ] Week-over-week crime trends
- [ ] Smart safety recommendations
- [ ] Incident type breakdowns
- [ ] Temporal analysis (peak crime times)

### **v2.0 (Future)**
- [ ] NOX Property (home value tracking)
- [ ] NOX Commute (transit intelligence)
- [ ] Referral system
- [ ] Multi-city expansion (Austin, Portland, Denver)

---

## 📝 License

Proprietary. All rights reserved.

---

## 🤝 Contributing

This is a commercial project. Not accepting contributions at this time.

---

## 💬 Support

Questions? Issues? Email: [your-email@domain.com]

---

## 🚀 Quick Start Summary

```bash
# 1. Clone repo
git clone [your-repo-url]
cd nox

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy .env.example to .env and fill in values
cp .env.example .env

# 4. Run locally
python bots/nox_crime.py

# 5. Test with /start in Telegram

# 6. Deploy to Render/Railway when ready
```

---

**Built with ☕ in Chicago**

**[Live Bot](https://t.me/your_bot_name)** • **[Twitter](https://twitter.com/nox_intel)** • **[Reddit](https://reddit.com/r/noxintel)**

---

*"See what's hidden."*