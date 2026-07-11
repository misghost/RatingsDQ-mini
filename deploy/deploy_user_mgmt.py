"""部署用户管理功能到生产服务器 163.7.4.36。
上传 db.py / server.py / webapp.html，触发迁移并重启 rating 服务。
"""
import os
import paramiko

HOST = "163.7.4.36"
USER = "root"
PWD = os.environ.get("DEPLOY_PWD") or os.environ.get("RATING_DEPLOY_PWD") or ""
REMOTE_DIR = "/opt/rating-engine"
LOCAL_DIR = "/Users/lion/WorkBuddy/2026-07-09-23-16-22/rating-engine"
FILES = ["db.py", "server.py", "webapp.html"]


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PWD, timeout=30)

    for f in FILES:
        local_path = os.path.join(LOCAL_DIR, f)
        remote_path = os.path.join(REMOTE_DIR, f)
        sftp = ssh.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()
        print("uploaded:", f)

    cmd = (
        f"cd {REMOTE_DIR} && "
        "set -a && source envfile && set +a && "
        "python3 -c 'import db; db.init_db()' && "
        "systemctl restart rating && sleep 2 && "
        "systemctl is-active rating"
    )
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    print("STDOUT:\n", out)
    if err:
        print("STDERR:\n", err)

    ssh.close()


if __name__ == "__main__":
    main()
