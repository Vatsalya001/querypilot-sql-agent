"""Deterministic sample data for the QueryPilot database (standard library only).

:func:`seed` fills an empty schema (``schema.sql``) using ``random.Random(42)``: the same code always
produces identical rows. Dates span 2015-2026 for employees and 2023-01-01..2026-09-30 for orders.

CLI: ``python -m app.db.seed [--reset] [--path PATH]`` builds the database (if needed) and prints row counts.
"""

from __future__ import annotations

import argparse
import random
import re
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import accumulate
from pathlib import Path

SEED = 42
DATA_START = date(2023, 1, 1)
DATA_END = date(2026, 9, 30)
ORDER_COUNT = 2000
DISCONTINUED_SALES_CUTOFF = date(2025, 1, 1)

FIRST_NAMES = (
    "Ava", "Liam", "Olivia", "Noah", "Emma", "Ethan", "Sophia", "Mason", "Isabella", "Lucas",
    "Mia", "Logan", "Amelia", "James", "Harper", "Benjamin", "Evelyn", "Elijah", "Abigail", "Oliver",
    "Emily", "Jacob", "Ella", "Michael", "Elizabeth", "Daniel", "Sofia", "Henry", "Avery", "Jackson",
    "Scarlett", "Sebastian", "Grace", "Aiden", "Chloe", "Matthew", "Victoria", "Samuel", "Riley", "David",
    "Aria", "Joseph", "Lily", "Carter", "Zoey", "Owen", "Penelope", "Wyatt", "Layla", "John",
    "Nora", "Jack", "Camila", "Luke", "Hannah", "Dylan", "Addison", "Grayson", "Priya", "Arjun",
    "Mei", "Wei", "Hiroshi", "Yuki", "Carlos", "Diego", "Valentina", "Mateo", "Lucia", "Omar",
    "Fatima", "Ahmed", "Aisha", "Kwame", "Amara", "Ivan", "Natalia", "Andre", "Chantal", "Raj",
    "Ananya", "Kenji", "Hana", "Luis", "Gabriela", "Thomas", "Rachel", "Nathan", "Megan", "Ryan",
    "Laura", "Kevin", "Sarah", "Brian", "Jessica", "Eric", "Nicole", "Marcus", "Tanya", "Victor",
)

LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts",
    "Patel", "Kim", "Chen", "Wang", "Singh", "Khan", "Tanaka", "Sato", "Kowalski", "Novak",
    "Okafor", "Mensah", "Cohen", "Murphy", "O'Brien", "Sullivan", "Bennett", "Brooks", "Foster", "Hayes",
    "Price", "Reed", "Russell", "Stewart", "Ward", "Watson", "Wood", "Cooper", "Bailey", "Bell",
    "Gomez", "Ortiz", "Morales", "Reyes", "Cruz", "Diaz", "Shah", "Gupta", "Ali", "Yamamoto",
    "Park", "Choi", "Ivanov", "Petrov", "Schmidt", "Muller", "Rossi", "Ferrari", "Dubois", "Laurent",
)

# (state, number of customers, ((city, area code), ...)); the counts sum to 300 and California leads.
STATES: tuple[tuple[str, int, tuple[tuple[str, str], ...]], ...] = (
    ("California", 45, (("Los Angeles", "213"), ("San Francisco", "415"), ("San Diego", "619"),
                        ("San Jose", "408"), ("Sacramento", "916"), ("Oakland", "510"))),
    ("Texas", 30, (("Houston", "713"), ("Dallas", "214"), ("Austin", "512"), ("San Antonio", "210"),
                   ("Fort Worth", "817"))),
    ("New York", 27, (("New York", "212"), ("Brooklyn", "718"), ("Buffalo", "716"), ("Rochester", "585"),
                      ("Albany", "518"))),
    ("Florida", 24, (("Miami", "305"), ("Orlando", "407"), ("Tampa", "813"), ("Jacksonville", "904"))),
    ("Illinois", 18, (("Chicago", "312"), ("Naperville", "630"), ("Springfield", "217"))),
    ("Washington", 15, (("Seattle", "206"), ("Bellevue", "425"), ("Spokane", "509"), ("Tacoma", "253"))),
    ("Massachusetts", 12, (("Boston", "617"), ("Cambridge", "617"), ("Worcester", "508"))),
    ("Pennsylvania", 12, (("Philadelphia", "215"), ("Pittsburgh", "412"), ("Allentown", "610"))),
    ("Georgia", 12, (("Atlanta", "404"), ("Savannah", "912"), ("Augusta", "706"))),
    ("Colorado", 12, (("Denver", "303"), ("Colorado Springs", "719"), ("Boulder", "303"))),
    ("Arizona", 9, (("Phoenix", "602"), ("Tucson", "520"), ("Scottsdale", "480"))),
    ("North Carolina", 9, (("Charlotte", "704"), ("Raleigh", "919"), ("Durham", "919"))),
    ("Ohio", 9, (("Columbus", "614"), ("Cleveland", "216"), ("Cincinnati", "513"))),
    ("Michigan", 9, (("Detroit", "313"), ("Grand Rapids", "616"), ("Ann Arbor", "734"))),
    ("Oregon", 9, (("Portland", "503"), ("Eugene", "541"), ("Salem", "503"))),
    ("New Jersey", 9, (("Newark", "973"), ("Jersey City", "201"), ("Princeton", "609"))),
    ("Virginia", 6, (("Richmond", "804"), ("Arlington", "703"), ("Virginia Beach", "757"))),
    ("Minnesota", 6, (("Minneapolis", "612"), ("Saint Paul", "651"))),
    ("Tennessee", 6, (("Nashville", "615"), ("Memphis", "901"), ("Knoxville", "865"))),
    ("Nevada", 6, (("Las Vegas", "702"), ("Reno", "775"))),
    ("Utah", 5, (("Salt Lake City", "801"), ("Provo", "801"))),
    ("Missouri", 4, (("Kansas City", "816"), ("St. Louis", "314"))),
    ("Wisconsin", 3, (("Milwaukee", "414"), ("Madison", "608"))),
    ("Maryland", 3, (("Baltimore", "410"), ("Annapolis", "410"))),
)

# (name, category, list price in USD)
PRODUCTS: tuple[tuple[str, str, float], ...] = (
    ("ProBook 14 Laptop", "Electronics", 1299.00),
    ("UltraLite 13 Laptop", "Electronics", 999.00),
    ("Workstation Tower X5", "Electronics", 2199.00),
    ("27-inch 4K Monitor", "Electronics", 429.00),
    ("24-inch Full HD Monitor", "Electronics", 179.00),
    ("34-inch Curved Monitor", "Electronics", 649.00),
    ("10-inch Tablet", "Electronics", 349.00),
    ("Smartphone S12", "Electronics", 799.00),
    ("Laser Printer Pro", "Electronics", 389.00),
    ("All-in-One Inkjet Printer", "Electronics", 149.00),
    ("Conference Room Projector", "Electronics", 899.00),
    ("Noise-Cancelling Headphones", "Electronics", 279.00),
    ("Wi-Fi 6 Router", "Electronics", 159.00),
    ("Document Scanner", "Electronics", 299.00),
    ("Ergonomic Office Chair", "Furniture", 349.00),
    ("Electric Standing Desk", "Furniture", 599.00),
    ("Executive Desk", "Furniture", 849.00),
    ("Conference Table (8-Seat)", "Furniture", 1249.00),
    ("4-Drawer Filing Cabinet", "Furniture", 289.00),
    ("5-Shelf Bookcase", "Furniture", 159.00),
    ("Guest Chair", "Furniture", 129.00),
    ("Reception Sofa", "Furniture", 999.00),
    ("Mobile Whiteboard", "Furniture", 219.00),
    ("LED Desk Lamp", "Furniture", 49.00),
    ("Copy Paper (Case of 10 Reams)", "Office Supplies", 54.99),
    ("Ballpoint Pens (12-Pack)", "Office Supplies", 8.99),
    ("Sticky Notes (24-Pack)", "Office Supplies", 17.49),
    ("Heavy-Duty Stapler", "Office Supplies", 24.99),
    ("Black Toner Cartridge", "Office Supplies", 89.99),
    ("Spiral Notebooks (5-Pack)", "Office Supplies", 12.99),
    ("Highlighters (10-Pack)", "Office Supplies", 9.49),
    ("File Folders (100-Pack)", "Office Supplies", 19.99),
    ("Envelopes (500-Pack)", "Office Supplies", 29.99),
    ("Desk Organizer", "Office Supplies", 22.99),
    ("Dry-Erase Markers (8-Pack)", "Office Supplies", 11.99),
    ("Paper Clips (1000-Pack)", "Office Supplies", 6.49),
    ("3-Ring Binders (6-Pack)", "Office Supplies", 26.99),
    ("Label Maker", "Office Supplies", 39.99),
    ("Office Suite (1-Year License)", "Software", 99.00),
    ("Antivirus Pro (1-Year)", "Software", 49.00),
    ("Project Management Suite (1-Year)", "Software", 240.00),
    ("Accounting Software", "Software", 399.00),
    ("Design Studio License", "Software", 599.00),
    ("Cloud Backup 1 TB (1-Year)", "Software", 69.00),
    ("Business VPN (1-Year)", "Software", 59.00),
    ("Video Conferencing Pro (1-Year)", "Software", 149.00),
    ("CRM Starter (1-Year)", "Software", 300.00),
    ("Password Manager Teams (1-Year)", "Software", 45.00),
    ("Wireless Mouse", "Accessories", 29.99),
    ("Mechanical Keyboard", "Accessories", 89.99),
    ("USB-C Hub (7-in-1)", "Accessories", 49.99),
    ("Laptop Backpack", "Accessories", 69.99),
    ("Dual Monitor Arm", "Accessories", 119.99),
    ("1080p Webcam", "Accessories", 79.99),
    ("USB Headset", "Accessories", 59.99),
    ("Adjustable Laptop Stand", "Accessories", 39.99),
    ("HDMI Cable (2 m)", "Accessories", 12.99),
    ("Thunderbolt Docking Station", "Accessories", 229.99),
    ("Wireless Charging Pad", "Accessories", 34.99),
    ("Portable SSD 1 TB", "Accessories", 109.99),
)

CATEGORY_POPULARITY = {"Electronics": 1.0, "Furniture": 0.6, "Office Supplies": 1.6, "Software": 0.9,
                       "Accessories": 1.4}
CATEGORY_MAX_QUANTITY = {"Electronics": 3, "Furniture": 4, "Office Supplies": 12, "Software": 10, "Accessories": 5}
SEGMENT_DISCOUNTS: dict[str, tuple[tuple[float, ...], tuple[int, ...]]] = {
    "Consumer": ((0.0, 0.05, 0.1), (70, 20, 10)),
    "Small Business": ((0.0, 0.05, 0.1, 0.15), (50, 25, 15, 10)),
    "Corporate": ((0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3), (30, 20, 20, 12, 8, 6, 4)),
}
PAYMENT_METHODS = ("Credit Card", "PayPal", "Bank Transfer", "Gift Card")
PAYMENT_WEIGHTS = {"Corporate": (40, 10, 45, 5), "Small Business": (50, 20, 22, 8), "Consumer": (55, 25, 8, 12)}


@dataclass(frozen=True)
class _Role:
    title: str
    salary: tuple[int, int]


@dataclass(frozen=True)
class _DepartmentSpec:
    name: str
    location: str
    budget: float
    head: _Role
    staff: tuple[_Role, ...]
    size: int  # including the head and the team leads
    lead: _Role | None = None
    leads: int = 0


DEPARTMENTS: tuple[_DepartmentSpec, ...] = (
    _DepartmentSpec(
        "Engineering", "San Francisco", 3_200_000, _Role("VP of Engineering", (195_000, 230_000)),
        (_Role("Software Engineer", (105_000, 140_000)), _Role("Senior Software Engineer", (140_000, 175_000)),
         _Role("Data Engineer", (120_000, 150_000)), _Role("DevOps Engineer", (115_000, 145_000)),
         _Role("QA Engineer", (85_000, 110_000))),
        20, _Role("Engineering Manager", (165_000, 190_000)), 2,
    ),
    _DepartmentSpec(
        "Sales", "New York", 1_900_000, _Role("VP of Sales", (175_000, 210_000)),
        (_Role("Account Executive", (75_000, 110_000)), _Role("Sales Representative", (55_000, 75_000)),
         _Role("Sales Development Representative", (50_000, 65_000))),
        16, _Role("Sales Manager", (120_000, 145_000)), 2,
    ),
    _DepartmentSpec(
        "Marketing", "Chicago", 1_100_000, _Role("Director of Marketing", (150_000, 180_000)),
        (_Role("Marketing Specialist", (60_000, 80_000)), _Role("Content Strategist", (65_000, 85_000)),
         _Role("Marketing Analyst", (70_000, 90_000))),
        8,
    ),
    _DepartmentSpec(
        "Finance", "New York", 800_000, _Role("Director of Finance", (160_000, 190_000)),
        (_Role("Financial Analyst", (75_000, 100_000)), _Role("Accountant", (65_000, 85_000))),
        6,
    ),
    _DepartmentSpec(
        "Human Resources", "Austin", 500_000, _Role("HR Director", (130_000, 155_000)),
        (_Role("HR Generalist", (60_000, 78_000)), _Role("Recruiter", (62_000, 82_000))),
        5,
    ),
    _DepartmentSpec(
        "Customer Support", "Denver", 700_000, _Role("Director of Customer Support", (125_000, 150_000)),
        (_Role("Support Specialist", (48_000, 62_000)), _Role("Customer Success Manager", (70_000, 90_000))),
        10, _Role("Support Team Lead", (80_000, 95_000)), 1,
    ),
    _DepartmentSpec(
        "Operations", "Seattle", 950_000, _Role("VP of Operations", (165_000, 195_000)),
        (_Role("Operations Analyst", (70_000, 90_000)), _Role("Logistics Coordinator", (55_000, 70_000))),
        6,
    ),
    _DepartmentSpec(
        "Product", "San Francisco", 1_400_000, _Role("Head of Product", (175_000, 205_000)),
        (_Role("Product Manager", (130_000, 160_000)), _Role("UX Designer", (100_000, 130_000)),
         _Role("Product Analyst", (90_000, 115_000))),
        8,
    ),
)
CEO = _Role("Chief Executive Officer", (285_000, 285_000))
CEO_DEPARTMENT = "Operations"
RECENT_HIRES = 16  # individual contributors hired on/after 2024-01-01
FORMER_EMPLOYEES = 6
SALES_DEPARTMENT = "Sales"


@dataclass
class _Person:
    key: int
    first: str
    last: str
    title: str
    department_id: int
    salary: float
    manager_key: int | None
    level: str  # "ceo", "head", "lead" or "staff"
    hire: date = DATA_START
    active: int = 1


@dataclass
class _Customer:
    customer_id: int
    first: str
    last: str
    email: str
    phone: str | None
    city: str
    state: str
    segment: str
    created: date
    weight: float


@dataclass
class _Ids:
    """Monotonic id counters for child rows."""

    order_item: int = 0
    payment: int = 0


def _random_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def _salary(rng: random.Random, bounds: tuple[int, int]) -> float:
    return float(round(rng.uniform(*bounds) / 500) * 500)


def _email_local(first: str, last: str) -> str:
    return re.sub(r"[^a-z.]", "", f"{first}.{last}".lower())


def _unique_email(local: str, domain: str, used: set[str]) -> str:
    candidate, suffix = f"{local}@{domain}", 2
    while candidate in used:
        candidate, suffix = f"{local}{suffix}@{domain}", suffix + 1
    used.add(candidate)
    return candidate


def _departments() -> list[tuple[int, str, str, float]]:
    return [(i, spec.name, spec.location, float(spec.budget)) for i, spec in enumerate(DEPARTMENTS, start=1)]


def _build_staff(rng: random.Random) -> list[_Person]:
    """CEO, department heads, team leads and individual contributors with a realistic reporting line."""
    dept_ids = {spec.name: i for i, spec in enumerate(DEPARTMENTS, start=1)}
    used_names: set[tuple[str, str]] = set()
    people: list[_Person] = []

    def new_person(level: str, role: _Role, dept_id: int, manager: _Person | None, hired: date | None) -> _Person:
        while True:
            name = (rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES))
            if name not in used_names:
                used_names.add(name)
                break
        salary = _salary(rng, role.salary)
        manager_key = manager.key if manager else None
        person = _Person(len(people), name[0], name[1], role.title, dept_id, salary, manager_key, level)
        if hired:
            person.hire = hired
        people.append(person)
        return person

    ceo = new_person("ceo", CEO, dept_ids[CEO_DEPARTMENT], None, date(2015, 1, 5))
    contributors: list[_Person] = []
    for dept_id, spec in enumerate(DEPARTMENTS, start=1):
        head = new_person("head", spec.head, dept_id, ceo, _random_date(rng, date(2015, 1, 15), date(2017, 12, 31)))
        leads = [
            new_person("lead", spec.lead, dept_id, head, _random_date(rng, date(2016, 1, 1), date(2021, 12, 31)))
            for _ in range(spec.leads)
            if spec.lead is not None
        ]
        for i in range(spec.size - 1 - len(leads)):
            manager = leads[i % len(leads)] if leads else head
            contributors.append(new_person("staff", spec.staff[i % len(spec.staff)], dept_id, manager, None))

    recent = rng.sample(range(len(contributors)), RECENT_HIRES)
    for index, person in enumerate(contributors):
        if index in recent:
            person.hire = _random_date(rng, date(2024, 1, 1), DATA_END)
        else:
            person.hire = _random_date(rng, date(2016, 3, 1), date(2023, 12, 31))
    veterans = [i for i in range(len(contributors)) if i not in recent]
    for index in rng.sample(veterans, FORMER_EMPLOYEES):
        contributors[index].active = 0
    return people


def _employee_rows(people: list[_Person], used_emails: set[str]) -> tuple[list[tuple], dict[int, int]]:
    """Assign EmployeeIDs in hire-date order (the CEO is employee 1) and remap manager keys."""
    ordered = sorted(people, key=lambda p: (p.hire, p.key))
    ids = {person.key: index for index, person in enumerate(ordered, start=1)}
    rows = []
    for person in ordered:
        email = _unique_email(_email_local(person.first, person.last), "acmeoffice.example", used_emails)
        manager_id = ids[person.manager_key] if person.manager_key is not None else None
        rows.append((ids[person.key], person.first, person.last, email, person.title, person.department_id,
                     manager_id, person.hire.isoformat(), person.salary, person.active))
    return rows, ids


def _build_customers(rng: random.Random, used_emails: set[str]) -> list[_Customer]:
    states = [state for state, count, _ in STATES for _ in range(count)]
    rng.shuffle(states)
    cities = {state: cities for state, _, cities in STATES}
    segments, segment_weights = ("Consumer", "Corporate", "Small Business"), (55, 28, 17)
    customers = []
    for customer_id, state in enumerate(states, start=1):
        options = cities[state]
        city, area = rng.choices(options, weights=range(len(options), 0, -1))[0]
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        domain = rng.choice(("example.com", "example.net", "example.org"))
        email = _unique_email(_email_local(first, last), domain, used_emails)
        phone = None if rng.random() < 0.04 else f"({area}) 555-01{rng.randint(0, 99):02d}"
        if rng.random() < 0.35:
            created = _random_date(rng, date(2022, 1, 1), date(2022, 12, 31))
        else:
            created = _random_date(rng, date(2023, 1, 1), date(2026, 8, 31))
        weight = 0.0 if rng.random() < 0.06 else rng.choice((0.5, 1.0, 1.0, 1.5, 2.0, 3.0, 5.0))
        segment = rng.choices(segments, weights=segment_weights)[0]
        customers.append(_Customer(customer_id, first, last, email, phone, city, state, segment, created, weight))
    return customers


def _customer_rows(customers: list[_Customer]) -> list[tuple]:
    return [(c.customer_id, c.first, c.last, c.email, c.phone, c.city, c.state, "USA", c.segment,
             c.created.isoformat()) for c in customers]


def _product_rows(rng: random.Random) -> list[tuple]:
    discontinued = set(rng.sample(range(len(PRODUCTS)), 5))
    rows = []
    for index, (name, category, price) in enumerate(PRODUCTS):
        cost = round(price * rng.uniform(0.45, 0.75), 2)
        stock = 0 if rng.random() < 0.05 else rng.randint(5, 400)
        rows.append((index + 1, name, category, price, cost, stock, 1 if index in discontinued else 0))
    return rows


def _order_status(rng: random.Random, order_date: date) -> str:
    age = (DATA_END - order_date).days
    if age <= 3:
        return rng.choices(("Pending", "Shipped", "Cancelled"), weights=(60, 35, 5))[0]
    if age <= 14:
        return rng.choices(("Pending", "Shipped", "Delivered", "Cancelled"), weights=(10, 70, 15, 5))[0]
    return rng.choices(("Delivered", "Cancelled", "Returned"), weights=(90, 6, 4))[0]


def _order_dates(rng: random.Random) -> list[date]:
    """Order dates with steady growth (late days are about twice as likely as early ones)."""
    span = (DATA_END - DATA_START).days + 1
    offsets = rng.choices(range(span), weights=[1.0 + day / span for day in range(span)], k=ORDER_COUNT)
    return [DATA_START + timedelta(days=offset) for offset in sorted(offsets)]


def _orders(
    rng: random.Random,
    customers: list[_Customer],
    reps: list[tuple[int, date, int]],
    products: list[tuple],
    ids: _Ids,
) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """Orders with their line items and payments."""
    buyers = sorted((c for c in customers if c.weight > 0), key=lambda c: (c.created, c.customer_id))
    created = [c.created for c in buyers]
    cumulative = list(accumulate(c.weight for c in buyers))
    all_cities = [(city, state) for state, _, cities in STATES for city, _ in cities]
    product_weights = [CATEGORY_POPULARITY[p[2]] * rng.uniform(0.5, 2.0) for p in products]
    orders, items, payments = [], [], []
    for order_id, order_date in enumerate(_order_dates(rng), start=1):
        eligible = max(1, bisect_right(created, order_date))
        customer = rng.choices(buyers[:eligible], cum_weights=cumulative[:eligible])[0]
        status = _order_status(rng, order_date)
        shipped = None
        if status not in ("Pending", "Cancelled"):
            shipped = min(order_date + timedelta(days=rng.randint(1, 4)), DATA_END).isoformat()
        city, state = (customer.city, customer.state) if rng.random() < 0.9 else rng.choice(all_cities)
        eligible_reps = [rep_id for rep_id, hired, active in reps
                         if hired <= order_date and (active or order_date < DISCONTINUED_SALES_CUTOFF)]
        rep = rng.choice(eligible_reps) if eligible_reps and rng.random() < 0.85 else None
        lines = _order_lines(rng, order_date, customer.segment, products, product_weights)
        total = round(sum(quantity * price * (1 - discount) for _, quantity, price, discount in lines), 2)
        for product_id, quantity, price, discount in lines:
            ids.order_item += 1
            items.append((ids.order_item, order_id, product_id, quantity, price, discount))
        orders.append((order_id, customer.customer_id, rep, order_date.isoformat(), shipped, status, city, state,
                       total))
        if status != "Cancelled":
            payments.extend(_payments(rng, ids, order_id, order_date, total, customer.segment))
    return orders, items, payments


def _order_lines(
    rng: random.Random, order_date: date, segment: str, products: list[tuple], weights: list[float]
) -> list[tuple[int, int, float, float]]:
    """1-4 distinct products (2.5 on average); discontinued products are only sold before 2025."""
    count = rng.choices((1, 2, 3, 4), weights=(20, 30, 30, 20))[0]
    usable = [(p, w) for p, w in zip(products, weights, strict=True)
              if not (p[6] and order_date >= DISCONTINUED_SALES_CUTOFF)]
    chosen: list[tuple] = []
    while len(chosen) < count:
        product = rng.choices([p for p, _ in usable], weights=[w for _, w in usable])[0]
        if product not in chosen:
            chosen.append(product)
    discounts, discount_weights = SEGMENT_DISCOUNTS[segment]
    lines = []
    for product in sorted(chosen):
        max_quantity = CATEGORY_MAX_QUANTITY[product[2]] * (2 if segment == "Corporate" else 1)
        discount = rng.choices(discounts, weights=discount_weights)[0]
        lines.append((product[0], rng.randint(1, max_quantity), product[3], discount))
    return lines


def _payments(
    rng: random.Random, ids: _Ids, order_id: int, order_date: date, total: float, segment: str
) -> list[tuple]:
    """One payment, or two installments (8%) that add up exactly to the order total."""
    method = rng.choices(PAYMENT_METHODS, weights=PAYMENT_WEIGHTS[segment])[0]
    paid = min(order_date + timedelta(days=rng.randint(0, 2)), DATA_END)
    if rng.random() >= 0.08 or total < 2:
        amounts = [(paid, total)]
    else:
        first = round(total * rng.choice((0.3, 0.5)), 2)
        later = min(paid + timedelta(days=rng.randint(15, 30)), DATA_END)
        amounts = [(paid, first), (later, round(total - first, 2))]
    rows = []
    for day, amount in amounts:
        ids.payment += 1
        rows.append((ids.payment, order_id, day.isoformat(), amount, method))
    return rows


def seed(conn: sqlite3.Connection) -> dict[str, int]:
    """Insert the deterministic sample data into an empty schema and return the row count per table."""
    rng = random.Random(SEED)
    ids = _Ids()
    used_emails: set[str] = set()
    departments = _departments()
    staff = _build_staff(rng)
    employees, key_to_id = _employee_rows(staff, used_emails)
    sales_id = next(i for i, spec in enumerate(DEPARTMENTS, start=1) if spec.name == SALES_DEPARTMENT)
    reps = sorted((key_to_id[p.key], p.hire, p.active) for p in staff
                  if p.department_id == sales_id and p.level in ("lead", "staff"))
    customers = _build_customers(rng, used_emails)
    products = _product_rows(rng)
    orders, items, payments = _orders(rng, customers, reps, products, ids)

    tables: dict[str, tuple[str, list[tuple]]] = {
        "Departments": ("INSERT INTO Departments VALUES (?, ?, ?, ?)", departments),
        "Employees": ("INSERT INTO Employees VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", employees),
        "Customers": ("INSERT INTO Customers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _customer_rows(customers)),
        "Products": ("INSERT INTO Products VALUES (?, ?, ?, ?, ?, ?, ?)", products),
        "Orders": ("INSERT INTO Orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", orders),
        "OrderItems": ("INSERT INTO OrderItems VALUES (?, ?, ?, ?, ?, ?)", items),
        "Payments": ("INSERT INTO Payments VALUES (?, ?, ?, ?, ?)", payments),
    }
    with conn:
        conn.execute("PRAGMA defer_foreign_keys = ON")  # managers may be hired after their reports
        for statement, rows in tables.values():
            conn.executemany(statement, rows)
    return {name: len(rows) for name, (_, rows) in tables.items()}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: build the database if needed (or rebuild with --reset) and print row counts."""
    # Imported here: app.db.database imports this module to seed new databases.
    from app.db.catalog import load_catalog
    from app.db.database import DEFAULT_DB_PATH, ensure_database

    parser = argparse.ArgumentParser(description="Build the QueryPilot sample SQLite database.")
    parser.add_argument("--reset", action="store_true", help="rebuild even if the database already exists")
    parser.add_argument("--path", type=Path, default=DEFAULT_DB_PATH, help="database file (default: %(default)s)")
    args = parser.parse_args(argv)
    path = ensure_database(args.path, reset=args.reset)
    catalog = load_catalog(path)
    print(f"Database: {path} ({path.stat().st_size / 1024:.0f} KiB)")
    for name in catalog.table_names():
        print(f"  {name:<12} {catalog.tables[name].row_count:>6,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
