-- QueryPilot sample database: a US retailer of office products (SQLite).
-- Conventions: PascalCase identifiers, dates are ISO-8601 TEXT 'YYYY-MM-DD', money is REAL in US dollars,
-- booleans are INTEGER 0/1. Built and seeded deterministically by app.db.database.ensure_database().

CREATE TABLE Departments (
    DepartmentID   INTEGER PRIMARY KEY,
    DepartmentName TEXT    NOT NULL,
    Location       TEXT    NOT NULL,
    Budget         REAL    NOT NULL CHECK (Budget >= 0)
);

CREATE TABLE Employees (
    EmployeeID   INTEGER PRIMARY KEY,
    FirstName    TEXT    NOT NULL,
    LastName     TEXT    NOT NULL,
    Email        TEXT    NOT NULL,
    JobTitle     TEXT    NOT NULL,
    DepartmentID INTEGER NOT NULL,
    ManagerID    INTEGER,
    HireDate     TEXT    NOT NULL CHECK (date(HireDate) IS HireDate),
    Salary       REAL    NOT NULL CHECK (Salary > 0),
    IsActive     INTEGER NOT NULL DEFAULT 1 CHECK (IsActive IN (0, 1)),
    FOREIGN KEY (DepartmentID) REFERENCES Departments (DepartmentID),
    FOREIGN KEY (ManagerID) REFERENCES Employees (EmployeeID)
);

CREATE TABLE Customers (
    CustomerID INTEGER PRIMARY KEY,
    FirstName  TEXT NOT NULL,
    LastName   TEXT NOT NULL,
    Email      TEXT NOT NULL,
    Phone      TEXT,
    City       TEXT NOT NULL,
    State      TEXT NOT NULL,
    Country    TEXT NOT NULL DEFAULT 'USA',
    Segment    TEXT NOT NULL CHECK (Segment IN ('Consumer', 'Corporate', 'Small Business')),
    CreatedAt  TEXT NOT NULL CHECK (date(CreatedAt) IS CreatedAt)
);

CREATE TABLE Products (
    ProductID      INTEGER PRIMARY KEY,
    ProductName    TEXT    NOT NULL,
    Category       TEXT    NOT NULL
        CHECK (Category IN ('Electronics', 'Furniture', 'Office Supplies', 'Software', 'Accessories')),
    UnitPrice      REAL    NOT NULL CHECK (UnitPrice > 0),
    UnitCost       REAL    NOT NULL CHECK (UnitCost > 0),
    StockQuantity  INTEGER NOT NULL CHECK (StockQuantity >= 0),
    IsDiscontinued INTEGER NOT NULL DEFAULT 0 CHECK (IsDiscontinued IN (0, 1))
);

CREATE TABLE Orders (
    OrderID       INTEGER PRIMARY KEY,
    CustomerID    INTEGER NOT NULL,
    EmployeeID    INTEGER,
    OrderDate     TEXT    NOT NULL CHECK (date(OrderDate) IS OrderDate),
    ShippedDate   TEXT CHECK (ShippedDate IS NULL OR (date(ShippedDate) IS ShippedDate AND ShippedDate >= OrderDate)),
    Status        TEXT    NOT NULL CHECK (Status IN ('Pending', 'Shipped', 'Delivered', 'Cancelled', 'Returned')),
    ShippingCity  TEXT    NOT NULL,
    ShippingState TEXT    NOT NULL,
    TotalAmount   REAL    NOT NULL CHECK (TotalAmount >= 0),
    FOREIGN KEY (CustomerID) REFERENCES Customers (CustomerID),
    FOREIGN KEY (EmployeeID) REFERENCES Employees (EmployeeID)
);

CREATE TABLE OrderItems (
    OrderItemID INTEGER PRIMARY KEY,
    OrderID     INTEGER NOT NULL,
    ProductID   INTEGER NOT NULL,
    Quantity    INTEGER NOT NULL CHECK (Quantity > 0),
    UnitPrice   REAL    NOT NULL CHECK (UnitPrice > 0),
    Discount    REAL    NOT NULL DEFAULT 0 CHECK (Discount BETWEEN 0 AND 0.3),
    FOREIGN KEY (OrderID) REFERENCES Orders (OrderID),
    FOREIGN KEY (ProductID) REFERENCES Products (ProductID)
);

CREATE TABLE Payments (
    PaymentID     INTEGER PRIMARY KEY,
    OrderID       INTEGER NOT NULL,
    PaymentDate   TEXT    NOT NULL CHECK (date(PaymentDate) IS PaymentDate),
    Amount        REAL    NOT NULL CHECK (Amount > 0),
    PaymentMethod TEXT    NOT NULL CHECK (PaymentMethod IN ('Credit Card', 'PayPal', 'Bank Transfer', 'Gift Card')),
    FOREIGN KEY (OrderID) REFERENCES Orders (OrderID)
);

-- Uniqueness (named indexes instead of anonymous sqlite_autoindex_* ones).
CREATE UNIQUE INDEX idx_departments_name ON Departments (DepartmentName);
CREATE UNIQUE INDEX idx_employees_email ON Employees (Email);
CREATE UNIQUE INDEX idx_customers_email ON Customers (Email);

-- Foreign-key and date lookups. Customers.State, Employees.HireDate, Products.Category and Orders.Status
-- are deliberately NOT indexed so that the optimizer has meaningful index recommendations to make.
CREATE INDEX idx_employees_department ON Employees (DepartmentID);
CREATE INDEX idx_orders_customer ON Orders (CustomerID);
CREATE INDEX idx_orders_date ON Orders (OrderDate);
CREATE INDEX idx_orderitems_order ON OrderItems (OrderID);
CREATE INDEX idx_orderitems_product ON OrderItems (ProductID);
CREATE INDEX idx_payments_order ON Payments (OrderID);
