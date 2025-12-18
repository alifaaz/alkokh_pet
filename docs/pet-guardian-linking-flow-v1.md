# 🐾 Pet Guardian Linking Flow (v1)
## Executive Overview – Non-Technical

**Project:** Pet_app
**Version:** v1.0  
**Audience:** Management / Product / Operations  
**Technology:** Frappe Framework (Standard APIs Only)

---

## 🎯 Purpose

This document explains **how pets are securely linked to their owners (Guardians)** in the mobile app, without technical complexity.

The goal is to:
- Prevent duplicate pet records
- Allow multiple family members to manage the same pet
- Keep full control with the clinic
- Make the mobile experience simple and safe

---

## 🧠 Core Idea (In Simple Terms)

- **The clinic creates the pet once** in the system
- **Owners do NOT create pets**
- Owners receive a **secure invite code** from the clinic
- Owners use this code in the mobile app to request access
- **The clinic approves or rejects** every request
- Approved owners can see and manage the pet in the app

---

## 👥 Who Is Who?

### 🏥 Clinic (Admin)
- Creates pets
- Generates first invite codes
- Approves or rejects owner requests
- Can remove or change owners at any time

### 👤 Guardian (Owner)
- Registers using phone number
- Can view only pets linked to them
- Can invite family members *after approval*
- Cannot create or modify pets

---

## 🔐 Why Invite Codes?

Instead of typing Pet IDs (which are predictable), the system uses:

- **One-time invite codes**
- Time-limited
- Can be revoked
- Prevents unauthorized access

This is the same model used by:
- Banking apps
- Family medical apps
- Smart home sharing apps

---

## 📱 Mobile App – Owner Journey

### Step 1: Register
- Guardian signs up with phone number
- OTP verification

### Step 2: Link a Pet
- Selects: “Link My Pet”
- Enters or scans invite code
- Sees pet details (name, type, photo)

### Step 3: Confirm
- Confirms: “Yes, this is my pet”
- Request is sent to clinic

### Step 4: Wait for Approval
- Status shows: “Pending”
- No access yet

### Step 5: Approved
- Clinic approves
- Pet appears automatically in the app

---

## 👨‍👩‍👧 Adding Family Members

- Primary owner can invite other family members
- Each invite is unique and controlled
- All additions require clinic approval
- Everyone sees the same pet data

---

## 🧑‍💼 Admin Dashboard – Clinic View

The clinic dashboard shows:

### ✔ Pending Requests
- Who is requesting access
- Which pet
- Phone number
- Request time

### ✔ Approval Actions
- Approve → owner gets access
- Reject → no access granted

### ✔ Full Control
- Remove any owner
- Disable access
- Resolve disputes

---

## 🔒 Safety & Control

The system guarantees:

✅ Pets cannot be duplicated  
✅ Owners cannot see pets they don’t own  
✅ No access without clinic approval  
✅ Invite codes cannot be reused  
✅ All actions are logged and auditable  

---

## 🧩 Why This Works

| Benefit | Value |
|------|------|
| Clinic control | No data chaos |
| Simple mobile flow | Easy for users |
| Secure linking | No guessing IDs |
| Scalable | Works for 1 or 1000 owners |
| No custom code | Low maintenance |

---

## 🚀 Ready for Phase 1

This flow is:
- Production ready
- Mobile friendly
- Approved pattern in healthcare & pet platforms
- Built entirely on standard system features

**Next phases** can add:
- Notifications
- Role levels (view-only, caretaker)
- QR code scanning
- Analytics

---

## ✅ Summary

> Pets are owned by the clinic  
> Access is granted by approval  
> Owners share pets securely  
> The system stays clean and controlled  

---

**Document:** pet-guardian-linking-flow-v1.md  
**Status:** Ready for Management Review  
**Last Updated:** December 2025  
