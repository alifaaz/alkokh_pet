# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
from frappe.utils import date_diff, getdate

class PetBoarding(Document):
    def validate(self):
        self.calculate_number_of_days()
        self.calculate_total_cost()
    
    def calculate_number_of_days(self):
        """Calculate number of days between check-in and check-out dates"""
        if self.check_in_date and self.check_out_date:
            # Calculate the difference in days
            days = date_diff(self.check_out_date, self.check_in_date)
            
            # Minimum 1 day even for same-day boarding
            self.number_of_days = max(days, 1)
    
    def calculate_total_cost(self):
        """Calculate total cost based on number of days and daily rate"""
        if self.number_of_days and self.daily_rate:
            self.total_cost = self.number_of_days * self.daily_rate
            
            # Calculate balance due if deposit was paid
            if self.deposit_amount:
                self.balance_due = self.total_cost - self.deposit_amount
            else:
                self.balance_due = self.total_cost
