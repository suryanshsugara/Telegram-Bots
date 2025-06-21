Contains the full bot logic, including:
- `/start`, `/help`, `/setprofile`, `/macros`, `/dietplan`, `/upgrade` commands
- Step-by-step onboarding system for weight, height, age, activity, goal, diet, cuisine, workout type
- Daily macro calculation based on BMR, TDEE and goal
- Premium access with UPI/PayPal
- Spoonacular meal plan generator for premium users
- Workout suggestion system by day

🔒 **All API keys and secrets are loaded from `.env`** like this:
```python
import os
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPOONACULAR_API_KEY = os.getenv("SPOONACULAR_API_KEY")
PAYPAL_LINK = os.getenv("PAYPAL_LINK")
UPI_ID = os.getenv("UPI_ID")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")
```