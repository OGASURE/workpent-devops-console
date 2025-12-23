import os
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

@app.get("/")
def home():
    return f"""
    <h1>{os.getenv("APP_NAME","SaaS Web App")} ✅</h1>
    <p>If you can see this, the app is running.</p>
    <p>Try <code>/health</code>.</p>
    """

@app.get("/health")
def health():
    return jsonify(status="ok", app=os.getenv("APP_NAME", "saas-web"))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
