Aljoud Attendance System (v2 Full)

Server:
- Copy server/ to /opt/aljoud/server
- cp .env.example .env and edit SECRET_KEY / AGENT_SHARED_SECRET
- python3 -m venv .venv && source .venv/bin/activate
- pip install -r requirements.txt
- systemd: cp systemd/aljoud.service /etc/systemd/system/ && systemctl enable --now aljoud.service

Default superadmin:
- username: admin
- password: 262992

Agent (Windows) is in agent/ (see agent/README.md)
