#!/usr/bin/env python3
# 部署 df.ratings.ink 受信任证书 + 切到域名（在收到腾讯云证书后运行）
# 用法:
#   CERT_FULL=/path/1_df.ratings.ink_bundle.crt \
#   CERT_KEY=/path/2_df.ratings.ink.key \
#   python3 apply_cert.py
import os, paramiko, sys

HOST="163.7.4.36"; USER="root"; PW="19870817Aa."
LOCAL=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

full=os.environ.get("CERT_FULL")
key=os.environ.get("CERT_KEY")
if not full or not key:
    print("缺少环境变量 CERT_FULL / CERT_KEY"); sys.exit(2)

ssh=paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PW, timeout=60)
sftp=ssh.open_sftp()

# 1) 证书
sftp.put(full, "/etc/ssl/rating/df.ratings.ink.fullchain.pem")
sftp.put(key,  "/etc/ssl/rating/df.ratings.ink.privkey.pem")
sftp.chmod("/etc/ssl/rating/df.ratings.ink.fullchain.pem", 0o644)
sftp.chmod("/etc/ssl/rating/df.ratings.ink.privkey.pem", 0o600)
print("cert uploaded")

# 2) 后端 + verify 目录（含微信校验路由）
sftp.put(f"{LOCAL}/server.py", "/opt/rating-engine/server.py")
sftp.chmod("/opt/rating-engine/server.py", 0o644)
# verify 目录整体上传
import shutil
for fn in os.listdir(f"{LOCAL}/verify"):
    sftp.put(f"{LOCAL}/verify/{fn}", f"/opt/rating-engine/verify/{fn}")
print("server.py + verify uploaded")

# 3) nginx 域名配置
sftp.put(f"{LOCAL}/deploy/nginx-domain.conf", "/etc/nginx/sites-available/rating-domain")
sftp.chmod("/etc/nginx/sites-available/rating-domain", 0o644)
try: sftp.remove("/etc/nginx/sites-enabled/rating-domain")
except: pass
sftp.symlink("/etc/nginx/sites-available/rating-domain", "/etc/nginx/sites-enabled/rating-domain")
print("nginx domain conf linked")

# 4) 校验 + 重载
def run(cmd):
    i,o,e=ssh.exec_command(cmd); return o.read().decode(), e.read().decode()
o,e=run("nginx -t 2>&1")
print("[nginx -t]\n", o, e)
if "test is successful" in o or "syntax is ok" in o.lower():
    o,e=run("systemctl reload nginx && echo RELOADED")
    print("[nginx reload]", o, e)
else:
    print("⚠️ nginx 校验失败，未重载，请检查证书路径/域名配置")
    ssh.close(); sys.exit(1)

# 5) 重启后端以加载校验路由
o,e=run("systemctl restart rating.service && sleep 1 && systemctl is-active rating.service")
print("[rating.service]", o.strip(), e.strip())
ssh.close()
print("DONE")
