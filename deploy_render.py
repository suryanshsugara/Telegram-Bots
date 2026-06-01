import os
import sys
import json
import urllib.request
import urllib.error

# Load environment variables from .env
def load_env(path=".env"):
    env = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("=", 1)
                if len(parts) == 2:
                    env[parts[0].strip()] = parts[1].strip()
    return env

def make_request(url, method="GET", headers=None, data=None):
    if headers is None:
        headers = {}
    
    req_data = None
    if data is not None:
        req_data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body
    except Exception as e:
        return 0, str(e)

def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    print("🚀 BookShook Bot — Render Deployment Automator")
    print("==============================================")
    
    env = load_env()
    
    # 1. Get Render API Key
    render_api_key = os.environ.get("RENDER_API_KEY")
    if not render_api_key:
        render_api_key = input("🔑 Enter your Render API Key (from https://dashboard.render.com/account): ").strip()
        if not render_api_key:
            print("❌ Error: Render API Key is required.")
            sys.exit(1)
            
    headers = {
        "Authorization": f"Bearer {render_api_key}",
        "Accept": "application/json"
    }
    
    # 2. Get Owner ID
    print("📡 Fetching Render Owner ID...")
    status, res = make_request("https://api.render.com/v1/owners?limit=1", headers=headers)
    if status != 200 or not res:
        print(f"❌ Failed to fetch owner ID (Status: {status}). Error: {res}")
        sys.exit(1)
        
    owner_id = res[0]["owner"]["id"]
    owner_email = res[0]["owner"]["email"]
    print(f"✅ Authenticated as: {owner_email} (Owner ID: {owner_id})")
    
    # 3. Get Repo URL
    # Try to find git remote URL
    git_repo = ""
    if os.path.exists(".git"):
        try:
            import subprocess
            res_git = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True, check=True)
            git_repo = res_git.stdout.strip()
        except Exception:
            pass
            
    if not git_repo:
        git_repo = input("📦 Enter your GitHub Repository URL (e.g. https://github.com/username/repo): ").strip()
        if not git_repo:
            print("❌ Error: GitHub Repository URL is required.")
            sys.exit(1)
            
    print(f"📁 Deploying from Git Repository: {git_repo}")
    branch = input("🌿 Enter target branch (default: main): ").strip() or "main"
    
    # Gather environment variables
    env_vars = [
        {"key": "TELEGRAM_BOT_TOKEN", "value": env.get("TELEGRAM_BOT_TOKEN", "")},
        {"key": "GOOGLE_API_KEY", "value": env.get("GOOGLE_API_KEY", "")},
        {"key": "GOOGLE_CSE_ID", "value": env.get("GOOGLE_CSE_ID", "")},
        {"key": "ADMIN_USER_ID", "value": env.get("ADMIN_USER_ID", "")},
        {"key": "DODO_PAYMENTS_API_KEY", "value": env.get("DODO_PAYMENTS_API_KEY", "")},
        {"key": "DODO_WEBHOOK_SECRET", "value": env.get("DODO_WEBHOOK_SECRET", "")},
        {"key": "DODO_PRODUCT_ID", "value": env.get("DODO_PRODUCT_ID", "")},
        {"key": "BOT_MODE", "value": "webhook"},
        {"key": "PORT", "value": "10000"},
        {"key": "WEBHOOK_URL", "value": "AUTO_ASSIGNED"}  # We'll update this once created
    ]
    
    # Prompt for missing required variables
    for var in env_vars[:4]:
        if not var["value"]:
            val = input(f"✏️ Enter value for {var['key']}: ").strip()
            var["value"] = val
            env[var["key"]] = val
            
    # Check if a BookShook web service already exists
    print("📡 Checking for existing services...")
    status, services = make_request(f"https://api.render.com/v1/services?ownerId={owner_id}&limit=20", headers=headers)
    existing_service = None
    if status == 200:
        for s in services:
            if s["service"]["name"] == "bookshook-bot":
                existing_service = s["service"]
                break
                
    payload = {
        "type": "web_service",
        "name": "bookshook-bot",
        "ownerId": owner_id,
        "repo": git_repo,
        "autoDeploy": "yes",
        "branch": branch,
        "serviceDetails": {
            "runtime": "python",
            "plan": "free",
            "region": "oregon",
            "envSpecificDetails": {
                "buildCommand": "pip install -r requirements.txt && python seed_data.py",
                "startCommand": "python BookShook.py"
            }
        },
        "envVars": env_vars
    }
    
    service_id = None
    service_url = None
    
    if existing_service:
        service_id = existing_service["id"]
        service_url = existing_service.get("serviceDetails", {}).get("url", "")
        print(f"⚠️ Found existing service 'bookshook-bot' (ID: {service_id})")
        choice = input("Do you want to update the existing service configuration and redeploy? (y/n): ").strip().lower()
        if choice == 'y':
            print("📡 Updating existing service variables...")
            # For update, we must set env vars endpoint
            status, res_update = make_request(f"https://api.render.com/v1/services/{service_id}/env-vars", method="PUT", headers=headers, data=env_vars)
            if status != 200:
                print(f"❌ Failed to update env vars (Status: {status}). Error: {res_update}")
                sys.exit(1)
            print("✅ Env vars updated. Triggering deploy...")
            status, res_deploy = make_request(f"https://api.render.com/v1/services/{service_id}/deploys", method="POST", headers=headers, data={})
            if status != 201:
                print(f"❌ Failed to trigger deploy (Status: {status}). Error: {res_deploy}")
        else:
            print("❌ Operation cancelled.")
            sys.exit(0)
    else:
        print("📡 Creating new Web Service 'bookshook-bot' on Render...")
        status, res_service = make_request("https://api.render.com/v1/services", method="POST", headers=headers, data=payload)
        if status != 201:
            print(f"❌ Failed to create service (Status: {status}). Error: {res_service}")
            sys.exit(1)
        
        service_id = res_service["service"]["id"]
        service_url = res_service["service"].get("serviceDetails", {}).get("url", "")
        print(f"✅ Web Service created successfully! Service ID: {service_id}")
        
    # 4. Set WEBHOOK_URL
    if service_url:
        print(f"🔗 Render Public URL: {service_url}")
        print("📡 Updating WEBHOOK_URL environment variable on Render...")
        
        # Replace the AUTO_ASSIGNED placeholder with actual URL
        for var in env_vars:
            if var["key"] == "WEBHOOK_URL":
                var["value"] = service_url
                
        # PUT env vars
        status, res_env = make_request(f"https://api.render.com/v1/services/{service_id}/env-vars", method="PUT", headers=headers, data=env_vars)
        if status == 200:
            print("✅ WEBHOOK_URL env var updated.")
        else:
            print(f"⚠️ Failed to update WEBHOOK_URL automatically. Please set WEBHOOK_URL={service_url} in Render dashboard.")
            
        # 5. Set Telegram Webhook
        bot_token = env.get("TELEGRAM_BOT_TOKEN")
        if bot_token:
            print("📡 Configuring Telegram Bot Webhook...")
            tg_url = f"https://api.telegram.org/bot{bot_token}/setWebhook?url={service_url}/webhook/{bot_token}&drop_pending_updates=True"
            status_tg, res_tg = make_request(tg_url)
            if status_tg == 200 and res_tg.get("ok"):
                print("✅ Telegram Webhook registered successfully!")
            else:
                print(f"⚠️ Failed to set Telegram webhook: {res_tg}")
                
        # 6. Dodo Payments Webhook Configuration Details
        print("\n==============================================")
        print("🎉 Render deployment configured successfully!")
        print("==============================================")
        print("👉 Final Step: Configure Dodo Payments Webhook")
        print("1. Go to your Dodo Payments Dashboard -> Developers -> Webhooks")
        print("2. Add new Webhook:")
        print(f"   - Webhook URL: {service_url}/dodo/webhook")
        print(f"   - Secret: Copy signing secret and set it as DODO_WEBHOOK_SECRET")
        print("   - Active Events:")
        print("     * payment.succeeded")
        print("==============================================")

if __name__ == "__main__":
    main()
