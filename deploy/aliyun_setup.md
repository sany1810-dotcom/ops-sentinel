# Alibaba Cloud Deployment — Step-by-Step

**Choice: ECS (Elastic Compute Service)**  
Reasons: SQLite needs persistent disk (Function Compute is stateless);
ECS is the simplest path with Docker Compose; free trial available.

---

## 1. Alibaba Cloud Account (your side)

1. Go to https://account.alibabacloud.com/register — create account with your email
2. Complete identity verification (passport or ID scan, takes ~10 min)
3. Add payment method (card needed for ECS, even on free trial)
4. Activate Free Trial: https://www.alibabacloud.com/free  
   → Look for "ECS t6/t7 1-core 1GB" — usually 1–3 months free in select regions

---

## 2. Create ECS Instance (your side)

1. Console → Elastic Compute Service → Instances → **Create Instance**
2. Settings:
   - **Region**: Singapore (ap-southeast-1) — lowest latency for international judges
   - **Instance type**: ecs.t6-c1m1.large (1 vCPU, 1 GB RAM) or ecs.s6-c1m2.small
   - **Image**: Ubuntu 22.04 LTS 64-bit
   - **Storage**: 40 GB system disk (default ESSD)
   - **Network**: VPC, assign public IP (Pay-As-You-Go bandwidth, 1 Mbps is enough)
   - **Security Group**: create new, open ports:
     - 22 (SSH)
     - 8000 (demo service)
     - 8001 (agent status page)
3. Set a **key pair** (download the .pem file — keep it safe)
4. Launch and note the **public IP address**

---

## 3. Connect & Prepare Server

```bash
# From your local machine (replace with your IP and key path)
ssh -i ~/your-key.pem root@<ECS_PUBLIC_IP>

# On the server:
apt update && apt upgrade -y
apt install -y docker.io docker-compose git

systemctl enable docker
systemctl start docker
```

---

## 4. Deploy Ops-Sentinel

```bash
# On the server:
git clone https://github.com/YOUR_USERNAME/ops-sentinel.git
cd ops-sentinel

# Create .env (NEVER commit this file)
cat > .env << 'EOF'
QWEN_API_KEY=your_actual_key_here
QWEN_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen3.6-flash
AGENT_POLL_INTERVAL=10
EOF

# Build and start
docker-compose -f deploy/docker-compose.yml --env-file .env up -d --build

# Verify
docker-compose -f deploy/docker-compose.yml ps
curl http://localhost:8000/health
curl http://localhost:8001/api/status
```

**Status page for judges:** `http://<ECS_PUBLIC_IP>:8001`  
**Demo service health:** `http://<ECS_PUBLIC_IP>:8000/health`

---

## 5. Smoke Test

```bash
# From anywhere (replace <IP> with your ECS public IP):

# 1. Check everything is healthy
curl http://<IP>:8000/health
curl http://<IP>:8001/api/status

# 2. Inject overload fault
curl -X POST http://<IP>:8000/inject \
  -H "Content-Type: application/json" \
  -d '{"fault":"overload"}'

# 3. Wait ~30s, watch agent logs
docker logs -f ops-agent

# 4. Inject the same fault AGAIN — agent should find it in memory
curl -X POST http://<IP>:8000/inject \
  -H "Content-Type: application/json" \
  -d '{"fault":"overload"}'

# 5. Check incidents in memory
curl http://<IP>:8001/api/incidents
```

---

## 6. Keep Running (optional: systemd auto-restart on reboot)

```bash
# On the server, already handled by Docker restart policy: unless-stopped
# After reboot: docker-compose -f deploy/docker-compose.yml up -d
```

---

## Cost Estimate

| Resource | Est. Cost |
|----------|-----------|
| ECS ecs.t6-c1m1.large (Singapore) | ~$6/month (or free trial) |
| Public IP (1 Mbps PayG) | ~$1/month |
| Outbound traffic (minimal) | ~$0.05 |
| Qwen API (qwen3.6-flash, ~1000 calls) | ~$0.10 |
| **Total** | **~$7/month** or free on trial |
