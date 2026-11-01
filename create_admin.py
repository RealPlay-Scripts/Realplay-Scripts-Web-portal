#!/usr/bin/env python3
"""
Run this script once to create the first admin user.
Usage: python create_admin.py
"""
from app import create_app
from app.models import db, User, Role

app = create_app()

with app.app_context():
    print("\n=== CFRP Whitelist — Create Admin ===\n")
    username = input("Admin username: ").strip()
    email = input("Admin email: ").strip()
    password = input("Admin password (min 8 chars): ").strip()

    if len(password) < 8:
        print("Password too short!")
        exit(1)

    existing = User.query.filter(
        (User.username == username) | (User.email == email)
    ).first()
    if existing:
        print(f"User '{existing.username}' already exists. Assigning admin role instead.")
        user = existing
    else:
        user = User(username=username, email=email)
        user.set_password(password)
        user.is_active = True
        user.email_verified = True
        db.session.add(user)
        db.session.flush()

    admin_role = Role.query.filter_by(name='admin').first()
    if admin_role and admin_role not in user.roles:
        user.roles.append(admin_role)

    member_role = Role.query.filter_by(name='member').first()
    if member_role and member_role not in user.roles:
        user.roles.append(member_role)

    db.session.commit()
    print(f"\n✓ Admin user '{user.username}' created/updated successfully.")
    print(f"  Login at: http://localhost:5000/auth/login\n")
