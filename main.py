import os
import uvicorn
from api import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("CHATGPT2API_HOST") or os.environ.get("HOST") or "0.0.0.0"
    port = int(os.environ.get("CHATGPT2API_PORT") or os.environ.get("PORT") or "3000")
    print(f"==================================================")
    print(f" ChatGPT2API (Web Edition)")
    print(f" - Web Console:  http://{host}:{port}/")
    print(f" - API Endpoint: http://{host}:{port}/v1")
    print(f"==================================================")
    uvicorn.run(app, host=host, port=port, access_log=True, log_level="info")
