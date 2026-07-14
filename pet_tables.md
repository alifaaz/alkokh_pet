### 1. CUSTOMER

naming_series | Data | Required | CUST-.####
customer_name | Data | Required | Full name
mobile | Data | Required | Primary contact
alternate_mobile | Data | | Secondary contact
email | Data | |
address | Link (Address) | | Default address
emergency_contact_name | Data | |
emergency_contact_mobile | Data | |
id_type | Select | | National ID/Passport/Driver License
id_number | Data | |
registration_date | Date | Required | Auto-set
customer_type | Select | | Individual/Organization
status | Select | Required | Active/Inactive
notes | Small Text | | Additional info

### 2. ADDRESS

naming_series | Data | Required | ADDR-.####
address_title | Data | Required | e.g., "Home", "Office"
address_line1 | Data | Required | Street address
address_line2 | Data | | Apartment, suite, etc
city | Data | Required |
state_province | Data | |
postal_code | Data | |
country | Link (Country) | Required |
is_primary | Check | | Primary address
address_type | Select | Required | Billing/Shipping/Both

### 3. CUSTOMER ADDRESSES (Child Table in Customer)

address | Link (Address) | Required |
is_primary | Check | | Primary address

### 4. PET

naming_series | Data | Required | PET-.####
pet_name | Data | Required |
species | Select | Required | Dog/Cat/Bird/Rabbit/Other
breed | Data | |
date_of_birth | Date | |
age | Data | | Auto-calculated (read-only)
gender | Select | | Male/Female/Unknown
color | Data | |
weight | Float | | Current weight in kg
microchip_number | Data | |
owner | Link (Customer) | Required | Primary owner
image | Attach Image | | Pet photo
status | Select | Required | Active/Deceased/Transferred
allergies | Small Text | | Known allergies
medical_notes | Text | | Chronic conditions, special needs
registration_date | Date | Required | Auto-set

### 5. PET APPOINTMENT

naming_series | Data | Required | APT-.YYYY.-.####
pet | Link (Pet) | Required |
owner | Link (Customer) | Required | Fetched from Pet (read-only)
owner_name | Data | | Fetched (read-only)
mobile | Data | | Fetched from Customer (read-only)
email | Data | | Fetched from Customer (read-only)
appointment_date | Date | Required |
appointment_time | Time | Required |
service_type | Select | Required | Consultation/Vaccination/Grooming/Surgery/Check-up
veterinarian | Link (Employee) | | Assigned doctor
duration_minutes | Int | | Expected duration (default 30)
status | Select | Required | Scheduled/Confirmed/Completed/Cancelled/No-Show
notes | Text | | Reason for visit
reminder_sent | Check | | If reminder was sent
created_by | Link (User) | | Auto
created_on | Datetime | | Auto

### 6. PET VISIT (Medical Record)

naming_series | Data | Required | VIS-.####
appointment | Link (Pet Appointment) | | Link to appointment if from appointment
pet | Link (Pet) | Required |
owner | Link (Customer) | Required | Fetched from Pet (read-only)
visit_date | Date | Required |
visit_time | Time | |
veterinarian | Link (Employee) | | Doctor who saw pet
weight | Float | | Weight in kg
temperature | Float | | Temperature in °C
heart_rate | Int | | Beats per minute
chief_complaint | Text | | Why they came
examination_findings | Text | | Physical exam notes
diagnosis | Text | | What was found
treatment_provided | Text | | What was done
services | Table (Visit Service) | Required | Services/procedures provided
prescriptions | Table (Visit Prescription) | | Medications prescribed
subtotal | Currency | | Sum of services
discount_amount | Currency | | Any discount
tax_amount | Currency | | Computed tax
total_amount | Currency | | Final amount
payment_status | Select | Required | Unpaid/Paid/Partial
amount_paid | Currency | | Amount received
status | Select | Required | Draft/Completed/Cancelled
next_visit_date | Date | | Follow-up date
follow_up_notes | Small Text | | Instructions for next visit
created_by | Link (User) | | Auto
created_on | Datetime | | Auto

### 7. VISIT SERVICE (Child Table)

service | Link (Service Type) | Required |
service_name | Data | | Auto-fetched (read-only)
description | Small Text | | Additional details
qty | Float | Required | Default 1
rate | Currency | Required | Unit price
amount | Currency | Required | qty × rate (computed)
performed_by | Link (Employee) | | Staff who performed

### 8. VISIT PRESCRIPTION (Child Table)

medication | Data | Required | Drug/medicine name
dosage | Data | Required | e.g., 250mg, 5ml
frequency | Data | Required | e.g., Twice daily, Every 8 hours
route | Select | | Oral/Topical/Injectable/Other
duration | Data | Required | e.g., 7 days, 2 weeks
quantity | Float | | Amount dispensed
instructions | Small Text | | How to administer

### 9. SERVICE TYPE (Master)

service_name | Data | Required | e.g., General Consultation, Vaccination
service_code | Data | | Internal code
service_category | Select | Required | Medical/Grooming/Boarding/Laboratory/Other
default_rate | Currency | | Standard price
duration_minutes | Int | | Typical duration
description | Small Text | | Service description
is_active | Check | Required | Active/Inactive

### 10. VACCINATION RECORD

naming_series | Data | Required | VAC-.####
pet | Link (Pet) | Required |
owner | Link (Customer) | Required | Fetched from Pet (read-only)
vaccine_name | Data | Required | e.g., Rabies, DHPP, FVRCP
vaccination_date | Date | Required |
next_due_date | Date | | When booster is due
veterinarian | Data | | Who administered
batch_number | Data | | Vaccine batch/lot number
manufacturer | Data | | Vaccine manufacturer
administration_site | Data | | Location on body
notes | Small Text | | Any reactions or comments
certificate_number | Data | | Official certificate

### 11. PET BOARDING (Optional)

naming_series | Data | Required | BRD-.####
pet | Link (Pet) | Required |
owner | Link (Customer) | Required | Fetched from Pet (read-only)
mobile | Data | | Fetched from Customer (read-only)
check_in | Datetime | | Set at check-in
check_out | Datetime | | Set at check-out
stay_hours | Float | | Elapsed hours, rounded up, minimum 1
stay_days | Float | | Elapsed 24-hour days, rounded up, minimum 1
boarding_type | Select | Required | Travel/Treatment
total_cost | Currency | | Sum of non-cancelled billable rows
deposit_amount | Currency | | Advance payment
balance_due | Currency | | Computed
status | Select | Required | Pending Room/Reserved/Checked In/Checked Out/Cancelled
cage_number | Data | | Assigned cage/kennel
special_instructions | Text | | Diet, medications, handling notes
food_provided | Check | | Using clinic food or owner's
feeding_schedule | Small Text | | Feeding times and amounts
