# 📌 Single File Upload API (Generic)  
This branch introduces a new **single-file upload mechanism** for all DocTypes in the system.  
It replaces the need for fieldname-based uploads and provides a clean, unified way of associating
one image with any document using only `doctype` and `docname`.

---

## 🚀 Features
### ✔ Upload one file only per document  
Automatically replaces old images attached to the same `doctype + docname`.

### ✔ No `fieldname` required  
The system links the uploaded file to the visual UI in Frappe using the `brand_photo` field (or any other logic you use later).

### ✔ SHA1 duplicate detection  
Prevents the same image from being uploaded twice even if the filename changes.

### ✔ Auto-delete old images  
When a new image is uploaded:
- old File records are removed  
- new file becomes the default image (`custom_is_default = 1`)

### ✔ Works with any DocType  
Currently linked to the FoodBrand image field (`brand_photo`), but can be extended to others.

---

## 🛠 API Endpoint

### **POST**  
/api/method/pet_app.api.pet.upload_single_file

yaml
Copy code

---

## 📥 Request (Form-Data)

| Key | Type | Description |
|------|--------|-------------|
| `file` | File | The image to upload |
| `doctype` | Text | Target DocType (e.g., `FoodBrand`) |
| `docname` | Text | Record name (e.g., `FB-0001`) |

---

## 📤 Successful Response

```json
{
  "message": "uploaded",
  "file": {
    "name": "FILE-00045",
    "file_url": "/files/1733346622-download.jpeg",
    "file_name": "1733346622-download.jpeg"
  }
}
If the image already exists:

json
Copy code
{
  "message": "duplicate",
  "file": {
    "name": "FILE-00044",
    "file_url": "/files/1733346000-logo.jpeg"
  }
}
🧠 Internal Logic Summary
Validate file and document existence

Compute SHA1 hash

Duplicate check:

If exists → return duplicate

Delete previous files attached to this docname

Insert the new file under Home/{doctype}

Update the DocType field:

ini
Copy code
brand_photo = file_url
Save file link to the field for UI consistency

Return final JSON result

📁 File Storage Location
Uploaded files are stored under:

Copy code
Home/{doctype}
Example:

Copy code
Home/FoodBrand
🧩 Extending in the Future
This system allows the following enhancements:

Auto-detect the correct image field inside any DocType

Support private file uploads

Support resizing or image compression

Add validation for allowed MIME types

Add max file size configuration

👨‍💻 Author
Mostafa Omar — Pet App Project
