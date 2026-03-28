# 🐾 Vet Management System

A modern veterinary clinic management system built using the Frappe Framework.
This system is designed to manage pets, veterinary visits, treatments, and clinic workflows efficiently with a clean and scalable architecture.

---

## 🚀 Overview

The Vet Management System provides a complete solution for veterinary clinics to handle:

* Pet records and medical history
* Veterinary visits and case sheets
* Medications and services
* API-based integrations
* Modular and maintainable backend structure

---

## ✨ Key Features

* 🐶 **Pet Management**
  Store and manage pet profiles, breeds, and medical data

* 🩺 **Vet Visits**
  Track visits, diagnoses, and treatments

* 📋 **Case Sheets**
  Structured medical records for each visit

* 💊 **Medications & Services**
  Manage prescriptions and services provided

* 🔗 **API First Design**
  Clean REST APIs for integration with frontend or mobile apps

* 🧹 **Clean Architecture**
  Refactored structure with removed legacy modules

---

## 🆕 What's New (v2)

* 🔥 Full system rebranding to Veterinary domain
* ❌ Removed leg*acy modules (appo*intments, old visits, services)
* 🆕 Introduced:

  * Vet Visit
  * Case Sheet
  * Visit Service & Medication Items
* ⚡ Improved API structure
* 🧱 Better modular design

---

## 🛠️ Tech Stack

* **Framework:** Frappe
* **Language:** Python
* **Database:** MariaDB
* **API:** REST

---

## 📂 Project Structure

```
pet_app/
├── api/
├── doctype/
│   ├── vet_visit/
│   ├── vet_case_sheet/
│   └── ...
├── patches/
├── public/
└── workspace/
```

---

## ⚙️ Installation

```bash
bench get-app pet_app
bench --site your-site install-app pet_app
```

---

## 🔄 Migration

After pulling updates:

```bash
bench migrate
```

---

## 👨‍💻 Author

Developed by **Ali And Mostafa 🚀**

---

## 📌 Notes

* This project is under active development
* APIs and structure may evolve with future updates

---
