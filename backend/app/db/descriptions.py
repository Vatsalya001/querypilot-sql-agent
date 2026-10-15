"""Business descriptions of the sample database (shown in the schema explorer and in LLM prompts).

Descriptions explain meaning, units, formats and allowed values that the DDL alone does not convey.
Keep them short: the full schema text is embedded in a cached system prompt.
"""

from __future__ import annotations

TABLE_DESCRIPTIONS: dict[str, str] = {
    "Departments": "Company departments with their office city and annual budget.",
    "Employees": "Company staff, including the sales reps assigned to orders; ManagerID gives the reporting line.",
    "Customers": "People and businesses that buy from the company (all in the USA).",
    "Products": "Product catalog with list price, unit cost and stock level.",
    "Orders": "One row per customer order: dates, status, shipping destination and order total.",
    "OrderItems": "Line items of each order: product, quantity, unit price and discount.",
    "Payments": "Payments received for orders; an order can be paid in several installments.",
}

COLUMN_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "Departments": {
        "DepartmentID": "Unique department id.",
        "DepartmentName": "Department name.",
        "Location": "Office city of the department.",
        "Budget": "Annual budget in USD.",
    },
    "Employees": {
        "EmployeeID": "Unique employee id.",
        "FirstName": "Employee first name.",
        "LastName": "Employee last name.",
        "Email": "Work email address.",
        "JobTitle": "Job title, e.g. 'Software Engineer', 'Sales Representative', 'Account Executive'.",
        "DepartmentID": "Department the employee works in.",
        "ManagerID": "EmployeeID of the direct manager; NULL for the CEO.",
        "HireDate": "Hire date 'YYYY-MM-DD'.",
        "Salary": "Annual salary in USD.",
        "IsActive": "1 = currently employed, 0 = former employee.",
    },
    "Customers": {
        "CustomerID": "Unique customer id.",
        "FirstName": "Customer (contact) first name.",
        "LastName": "Customer (contact) last name.",
        "Email": "Customer email address.",
        "Phone": "Phone number like '(415) 555-0142'; may be NULL.",
        "City": "City name, e.g. 'Los Angeles'.",
        "State": "Full US state name, never an abbreviation (e.g. 'California', not 'CA').",
        "Country": "Country; always 'USA'.",
        "Segment": "Customer segment: 'Consumer', 'Corporate' or 'Small Business'.",
        "CreatedAt": "Date the customer account was created, 'YYYY-MM-DD'.",
    },
    "Products": {
        "ProductID": "Unique product id.",
        "ProductName": "Product name.",
        "Category": "One of 'Electronics', 'Furniture', 'Office Supplies', 'Software', 'Accessories'.",
        "UnitPrice": "Current list price per unit in USD.",
        "UnitCost": "Cost per unit in USD (margin = UnitPrice - UnitCost).",
        "StockQuantity": "Units in stock; 0 = out of stock.",
        "IsDiscontinued": "1 = no longer sold, 0 = active product.",
    },
    "Orders": {
        "OrderID": "Unique order id.",
        "CustomerID": "Customer who placed the order.",
        "EmployeeID": "Sales rep who handled the order; NULL for online self-service orders.",
        "OrderDate": "Order date 'YYYY-MM-DD'.",
        "ShippedDate": "Ship date 'YYYY-MM-DD'; NULL while Pending or if Cancelled.",
        "Status": "One of 'Pending', 'Shipped', 'Delivered', 'Cancelled', 'Returned'.",
        "ShippingCity": "Destination city.",
        "ShippingState": "Destination state, full US state name.",
        "TotalAmount": "Order total in USD = SUM(Quantity * UnitPrice * (1 - Discount)) of its items.",
    },
    "OrderItems": {
        "OrderItemID": "Unique line item id.",
        "OrderID": "Order this line belongs to.",
        "ProductID": "Product sold on this line.",
        "Quantity": "Units sold.",
        "UnitPrice": "Price per unit in USD at the time of the order.",
        "Discount": "Discount fraction from 0 to 0.3 (0.1 = 10% off).",
    },
    "Payments": {
        "PaymentID": "Unique payment id.",
        "OrderID": "Order being paid.",
        "PaymentDate": "Payment date 'YYYY-MM-DD'.",
        "Amount": "Amount paid in USD.",
        "PaymentMethod": "One of 'Credit Card', 'PayPal', 'Bank Transfer', 'Gift Card'.",
    },
}

# Everyday words -> tables they refer to (schema retrieval and "did you mean" suggestions).
TERM_TABLES: dict[str, tuple[str, ...]] = {
    "employee": ("Employees",), "staff": ("Employees",), "worker": ("Employees",), "hire": ("Employees",),
    "hired": ("Employees",), "salary": ("Employees",), "manager": ("Employees",), "rep": ("Employees",),
    "salesperson": ("Employees",), "job": ("Employees",), "title": ("Employees",),
    "customer": ("Customers",), "client": ("Customers",), "buyer": ("Customers",), "state": ("Customers",),
    "city": ("Customers",), "segment": ("Customers",),
    "order": ("Orders", "OrderItems"), "sale": ("Orders", "OrderItems"), "sold": ("Orders", "OrderItems"),
    "purchase": ("Orders", "OrderItems"), "revenue": ("Orders", "OrderItems"), "shipped": ("Orders",),
    "bought": ("Orders", "OrderItems"), "spent": ("Orders",), "spend": ("Orders",), "shipping": ("Orders",),
    "lineitem": ("OrderItems",), "item": ("Products",), "goods": ("Products",), "product": ("Products",),
    "category": ("Products",), "stock": ("Products",), "inventory": ("Products",), "price": ("Products",),
    "team": ("Departments",), "dept": ("Departments",), "department": ("Departments",), "budget": ("Departments",),
    "payment": ("Payments",), "paid": ("Payments",), "transaction": ("Payments",), "method": ("Payments",),
    "installment": ("Payments",),
}

# Common wrong column names -> the columns that most likely were meant ("did you mean" suggestions).
COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "region": ("State",), "province": ("State",), "territory": ("State",),
    "name": ("FirstName", "LastName", "ProductName", "DepartmentName"), "fullname": ("FirstName", "LastName"),
    "price": ("UnitPrice",), "cost": ("UnitCost",), "amount": ("TotalAmount", "Amount"),
    "total": ("TotalAmount", "Amount"), "revenue": ("TotalAmount",), "sales": ("TotalAmount",),
    "date": ("OrderDate", "HireDate", "PaymentDate", "CreatedAt"), "qty": ("Quantity", "StockQuantity"),
    "units": ("Quantity", "StockQuantity"), "stock": ("StockQuantity",), "inventory": ("StockQuantity",),
    "dept": ("DepartmentID", "DepartmentName"), "department": ("DepartmentID", "DepartmentName"),
    "manager": ("ManagerID",), "boss": ("ManagerID",), "supervisor": ("ManagerID",),
    "hired": ("HireDate",), "startdate": ("HireDate",), "joindate": ("HireDate", "CreatedAt"),
    "title": ("JobTitle",), "position": ("JobTitle",), "role": ("JobTitle",), "job": ("JobTitle",),
    "active": ("IsActive",), "status": ("Status", "IsActive"), "type": ("Category", "Segment", "PaymentMethod"),
    "method": ("PaymentMethod",), "paymenttype": ("PaymentMethod",), "phonenumber": ("Phone",),
    "telephone": ("Phone",), "mobile": ("Phone",), "mail": ("Email",), "emailaddress": ("Email",),
    "created": ("CreatedAt",), "signupdate": ("CreatedAt",), "registered": ("CreatedAt",),
    "shipdate": ("ShippedDate",), "shippingdate": ("ShippedDate",), "shipped": ("ShippedDate",),
    "wage": ("Salary",), "pay": ("Salary",), "compensation": ("Salary",), "income": ("Salary",),
    "location": ("Location", "City"), "office": ("Location",), "discountrate": ("Discount",),
    "customer": ("CustomerID",), "client": ("CustomerID",), "employee": ("EmployeeID",),
    "salesrep": ("EmployeeID",), "rep": ("EmployeeID",), "product": ("ProductID",), "order": ("OrderID",),
}
