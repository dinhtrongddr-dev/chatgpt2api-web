# ChatGPT2API (Web Edition) 🚀

> **Biến ChatGPT Web thành API chuẩn OpenAI-compatible siêu nhẹ, chuyên dụng để cấp API cho mọi ứng dụng AI.**

Phiên bản này được đóng gói tối ưu hóa theo hướng **Web Only** (chỉ sử dụng giao thức ChatGPT Web reverse proxy, lược bỏ router API Codex và các thành phần nặng không cần thiết). Toàn bộ dashboard quản trị đã được build sẵn, giúp ứng dụng cực kỳ nhẹ (~4.5 MB mã nguồn), khởi động tức thì và có thể cài đặt trên **mọi hệ điều hành VPS** (Ubuntu, Debian, CentOS, AlmaLinux, Rocky Linux, Alpine, Arch, macOS, Windows Server).

---

## 🌟 Tính Năng Nổi Bật

- ⚡ **Web-Only & Siêu Nhẹ**: Loại bỏ toàn bộ dependency Codex và Node.js runtime khi chạy. Chỉ cần Python hoặc Docker.
- 🔄 **Tương Thích Tuyệt Đối Với OpenAI API**:
  - `GET /v1/models`: Tự động đồng bộ danh mục model từ tài khoản ChatGPT đang hoạt động.
  - `POST /v1/chat/completions`: Hỗ trợ đầy đủ Streaming SSE (`stream: true`), non-streaming, multi-turn chat.
  - `POST /v1/images/generations`: Tạo ảnh (`gpt-image-2`, etc.).
  - `POST /v1/messages`: Hỗ trợ cả định dạng Anthropic Claude API.
- 🎯 **Hỗ Trợ Toàn Bộ Model ChatGPT Web Mới Nhất**:
  - `gpt-6-astra-wm`, `gpt-6.1-sol-wm`, `gpt-6-thinking`
  - `gpt-5-6`, `gpt-5-thinking`
  - `gpt-4o`, `gpt-4o-mini`, `gpt-4`
  - Hỗ trợ chế độ suy luận chuyên sâu (Thinking Effort: `low`, `medium`, `high`, `extended`/`max`).
- 👥 **Quản Trị Đa Tài Khoản (Multi-Account Pool)**:
  - Tự động xoay vòng tài khoản (load-balancing).
  - Tự động làm mới access token định kỳ qua refresh token.
  - Tự động xử lý khi một tài khoản chạm giới hạn (rate-limit / quota).
- 🖥️ **Web Dashboard Tích Hợp Sẵn**:
  - Đăng nhập và quản lý tài khoản ChatGPT qua giao diện trực quan (OAuth Login hoặc nhập Access Token).
  - Tạo và phân quyền API Key cho các ứng dụng khác nhau.
  - Theo dõi nhật ký cuộc gọi (logs), thời gian phản hồi (latency), số token và đồ thị thống kê.

---

## 🛠️ Hướng Dẫn Cài Đặt Trên VPS (Mọi Hệ Điều Hành)

### Cách 1: Sử dụng Docker & Docker Compose (Khuyên dùng - Nhanh & Chuẩn nhất)

Phương pháp này hoạt động trên **tất cả các bản phân phối Linux, macOS và Windows Server** đã cài Docker.

```bash
# 1. Clone repository từ GitHub
git clone https://github.com/<your-username>/chatgpt2api-web.git
cd chatgpt2api-web

# 2. Tạo file cấu hình môi trường
cp .env.example .env

# 3. Mở file .env và đặt khóa bí mật quản trị (CHATGPT2API_AUTH_KEY)
nano .env

# 4. Khởi chạy container ngầm trong nền
docker compose up -d --build
```

Kiểm tra trạng thái container:
```bash
docker compose ps
docker compose logs -f
```

- **Giao diện quản trị (Dashboard)**: `http://<IP-VPS>:3000/`
- **Endpoint API cho các app gọi**: `http://<IP-VPS>:3000/v1`

---

### Cách 2: Cài Đặt Tự Động Bằng Script (`install.sh`)

Script tự động nhận diện hệ điều hành Linux (Ubuntu, Debian, CentOS, RHEL, AlmaLinux, Alpine, Arch...), cài đặt các gói phụ thuộc và cấu hình service tự chạy khi VPS khởi động lại:

```bash
cd chatgpt2api-web
bash deploy/install.sh
```

---

### Cách 3: Cài Đặt Trực Tiếp Bằng Python 3 (Native)

Dành cho VPS không muốn dùng Docker:

```bash
# Cài đặt Python 3.11/3.12/3.13 và thư viện hệ thống (ví dụ trên Ubuntu/Debian):
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git curl

# Clone và di chuyển vào thư mục:
git clone https://github.com/<your-username>/chatgpt2api-web.git
cd chatgpt2api-web

# Tạo virtual environment:
python3 -m venv .venv
source .venv/bin/activate

# Cài đặt dependencies:
pip install -r requirements.txt

# Cấu hình biến môi trường:
cp .env.example .env
nano .env

# Chạy server:
python main.py
```

> **Chạy ngầm với Systemd trên Linux**:
> ```bash
> sudo cp deploy/chatgpt2api.service /etc/systemd/system/
> sudo systemctl daemon-reload
> sudo systemctl enable --now chatgpt2api
> sudo systemctl status chatgpt2api
> ```

---

## ⚙️ Cấu Hình Ban Đầu

1. Mở trình duyệt và truy cập: `http://<IP-VPS>:3000/login`
2. Đăng nhập bằng mã `CHATGPT2API_AUTH_KEY` bạn đã thiết lập trong file `.env`.
3. Vào mục **Tài khoản (Accounts)**:
   - Bấm **Thêm tài khoản** / **Đăng nhập OAuth** để liên kết tài khoản ChatGPT (hỗ trợ Free, Plus, Team, Pro).
   - Hoặc dán trực tiếp Refresh Token / Access Token.
4. Sau khi thêm tài khoản thành công, hệ thống sẽ tự động đồng bộ danh sách model vào cache.

---

## 🔌 Cách Kết Nối Các Ứng Dụng Khác Vào Service Này

Service này đóng vai trò là **nhà cung cấp API (API Provider)** tiêu chuẩn. Mọi ứng dụng có hỗ trợ OpenAI API đều có thể kết nối ngay lập tức:

### Thông số kết nối chung:
- **API Base URL**: `http://<IP-VPS>:3000/v1` (hoặc domain HTTPS nếu có Nginx reverse proxy)
- **API Key**: Nhập mã `CHATGPT2API_AUTH_KEY` (hoặc API Key bạn tự tạo trong Dashboard)
- **Model**: `gpt-6-astra-wm`, `gpt-6.1-sol-wm`, `gpt-6-thinking`, `gpt-5-6`, `gpt-4o`...

---

### 1. NextChat (ChatGPT-Next-Web)
Trong phần **Settings** (Cài đặt):
- **Model Provider**: `OpenAI`
- **OpenAI Endpoint**: `http://<IP-VPS>:3000/v1`
- **API Key**: `<CHATGPT2API_AUTH_KEY>`
- **Custom Models**: Thêm `gpt-6-astra-wm,gpt-6-thinking,gpt-5-6`

---

### 2. Open WebUI / LibreChat / LobeChat
- **OPENAI_API_BASE_URL**: `http://<IP-VPS>:3000/v1`
- **OPENAI_API_KEY**: `<CHATGPT2API_AUTH_KEY>`

---

### 3. Cursor / VS Code (Continue, Roo Code, Cline)
- Chọn provider: **OpenAI Compatible**
- **Base URL**: `http://<IP-VPS>:3000/v1`
- **API Key**: `<CHATGPT2API_AUTH_KEY>`
- **Model ID**: `gpt-6-astra-wm` hoặc `gpt-5-6`

---

### 4. Gọi Bằng Python SDK (`openai`)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://<IP-VPS>:3000/v1",
    api_key="sk-your-super-secret-admin-key-here"
)

# Chat streaming
response = client.chat.completions.create(
    model="gpt-6-astra-wm",
    messages=[
        {"role": "user", "content": "Xin chào, hãy giới thiệu ngắn gọn về bạn!"}
    ],
    stream=True
)

for chunk in response:
    content = chunk.choices[0].delta.content or ""
    print(content, end="", flush=True)
print()
```

---

### 5. Gọi Bằng cURL

```bash
curl http://<IP-VPS>:3000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-super-secret-admin-key-here" \
  -d '{
    "model": "gpt-6-astra-wm",
    "messages": [
      {"role": "user", "content": "Hello world"}
    ],
    "stream": false
  }'
```

---

## 🔒 Khuyến Nghị Triển Khai Thực Tế Trên VPS

1. **Mở Port Firewall trên VPS**:
   ```bash
   sudo ufw allow 3000/tcp
   ```
2. **Cấu hình Nginx Reverse Proxy + SSL (HTTPS Miễn phí qua Certbot)**:
   Để các app bên ngoài gọi an toàn qua HTTPS:
   ```nginx
   server {
       listen 80;
       server_name api.yourdomain.com;

       location / {
           proxy_pass http://127.0.0.1:3000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;

           # Bắt buộc cho SSE Streaming
           proxy_buffering off;
           proxy_cache off;
           proxy_read_timeout 600s;
       }
   }
   ```
   Sau đó chạy `certbot --nginx -d api.yourdomain.com` để có HTTPS.

---

## 📄 License
Dự án được phân phối dưới giấy phép **AGPL-3.0 License**.
