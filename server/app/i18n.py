from flask import session, current_app

AR = {
  "login": "تسجيل الدخول",
  "username": "اسم المستخدم",
  "password": "كلمة المرور",
  "logout": "تسجيل خروج",
  "branches": "الفروع",
  "employees": "الموظفين",
  "devices": "الأجهزة والشبكة",
  "branding": "الهوية البصرية",
  "exports": "التصدير",
  "backups": "النسخ الاحتياطي",
  "roles": "الأدوار والصلاحيات",
  "reports": "التقارير",
  "pending": "ضربات بانتظار الموافقة",
  "approve": "موافقة",
  "reject": "رفض",
  "save": "حفظ",
  "create": "إضافة",
  "update": "تعديل",
  "delete": "حذف",
  "test": "فحص",
  "sync_users": "مزامنة الموظفين مع الأجهزة",
  "developed_by": "Developed by Ibrahim Zartit – Aljoud.de",
}
DE = {
  "login": "Anmelden",
  "username": "Benutzername",
  "password": "Passwort",
  "logout": "Abmelden",
  "branches": "Filialen",
  "employees": "Mitarbeiter",
  "devices": "Geräte & Netzwerk",
  "branding": "Branding",
  "exports": "Exporte",
  "backups": "Backups",
  "roles": "Rollen & Berechtigungen",
  "reports": "Berichte",
  "pending": "Ausstehende Buchungen",
  "approve": "Genehmigen",
  "reject": "Ablehnen",
  "save": "Speichern",
  "create": "Erstellen",
  "update": "Aktualisieren",
  "delete": "Löschen",
  "test": "Testen",
  "sync_users": "Mitarbeiter mit Geräten synchronisieren",
  "developed_by": "Developed by Ibrahim Zartit – Aljoud.de",
}

def get_locale() -> str:
    return session.get("locale") or current_app.config.get("DEFAULT_LOCALE", "ar")

def t(key: str) -> str:
    d = AR if get_locale() == "ar" else DE
    return d.get(key, key)
