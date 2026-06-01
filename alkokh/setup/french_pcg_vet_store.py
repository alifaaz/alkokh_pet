"""Idempotent French-PCG-style setup for Alkokh Vet Store.

Safe bench execute:
	bench --site <site> execute alkokh.setup.french_pcg_vet_store.setup_french_pcg_vet_store --kwargs "{'company':'Kokh-vet'}"
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import frappe


DEFAULT_COMPANY = "Kokh-vet"
CUSTOMER_NAME = "عميل نقدي / Walk-in Customer"
POS_PROFILE_NAME = "Alkokh Vet Store - Main Cashier"
LOGGER_NAME = "alkokh_french_pcg_setup"

LOGGER = frappe.logger(LOGGER_NAME)
_ACTIVE_SUMMARY: dict[str, Any] | None = None
_META_CACHE: dict[str, Any] = {}
_COMPANY_ABBR_CACHE: dict[str, str] = {}

REQUIRED_META_DOCTYPES = (
	"Account",
	"Company",
	"Mode of Payment",
	"POS Profile",
	"Warehouse",
	"Item Group",
	"Item Default",
	"Customer",
	"Customer Group",
	"Supplier Group",
	"Sales Taxes and Charges Template",
	"Purchase Taxes and Charges Template",
	"Cost Center",
	"Accounting Dimension",
)

VALID_ROOT_TYPES = {"Equity", "Liability", "Asset", "Expense", "Income"}

# ERPNext validates Account.account_number as unique per company. These broad PCG
# buckets are repeated in the requested balance-sheet tree, so duplicate parent
# buckets use their nearest unique PCG-compatible subgroup number while ledgers
# keep the exact requested PCG numbers.
DUPLICATE_GROUP_NUMBER_WARNINGS = (
	"ERPNext requires Account.account_number to be unique per company; asset-side duplicate group '40' is created as PCG subgroup '409'.",
	"ERPNext requires Account.account_number to be unique per company; liability-side customer advances group '41' is created as PCG subgroup '419' and asset-side receivables as '411'.",
	"ERPNext requires Account.account_number to be unique per company; asset-side duplicate group '42' is created as PCG subgroup '425'.",
	"ERPNext requires Account.account_number to be unique per company; asset-side duplicate group '44' is created as PCG subgroup '445'.",
	"ERPNext requires Account.account_number to be unique per company; asset-side duplicate group '48' is created as PCG subgroup '486'.",
)


ACCOUNT_ROWS: tuple[dict[str, Any], ...] = (
	# Equity / حقوق الملكية
	{"number": "10", "name": "رأس المال والاحتياطيات", "root_type": "Equity", "parent": None, "is_group": 1},
	{"number": "101000", "name": "رأس المال", "root_type": "Equity", "parent": "10", "account_type": "Equity"},
	{"number": "106100", "name": "الاحتياطي القانوني", "root_type": "Equity", "parent": "10", "account_type": "Equity"},
	{"number": "106800", "name": "احتياطيات أخرى", "root_type": "Equity", "parent": "10", "account_type": "Equity"},
	{"number": "11", "name": "الأرباح أو الخسائر المرحلة", "root_type": "Equity", "parent": None, "is_group": 1},
	{"number": "110000", "name": "أرباح مرحلة دائنة", "root_type": "Equity", "parent": "11", "account_type": "Equity"},
	{"number": "119000", "name": "خسائر مرحلة مدينة", "root_type": "Equity", "parent": "11", "account_type": "Equity"},
	{"number": "12", "name": "نتيجة السنة المالية", "root_type": "Equity", "parent": None, "is_group": 1},
	{"number": "120000", "name": "نتيجة السنة المالية - ربح", "root_type": "Equity", "parent": "12", "account_type": "Equity"},
	{"number": "129000", "name": "نتيجة السنة المالية - خسارة", "root_type": "Equity", "parent": "12", "account_type": "Equity"},
	{"number": "108", "name": "حساب المالك / صاحب المشروع", "root_type": "Equity", "parent": None, "is_group": 1},
	{"number": "108000", "name": "حساب المالك / المسحوبات والإيداعات الشخصية", "root_type": "Equity", "parent": "108", "account_type": "Equity"},
	# Liability / الالتزامات
	{"number": "15", "name": "المخصصات", "root_type": "Liability", "parent": None, "is_group": 1},
	{"number": "151100", "name": "مخصصات نزاعات أو دعاوى", "root_type": "Liability", "parent": "15"},
	{"number": "151500", "name": "مخصصات ضمانات العملاء أو المنتجات", "root_type": "Liability", "parent": "15"},
	{"number": "16", "name": "القروض والديون المشابهة", "root_type": "Liability", "parent": None, "is_group": 1},
	{"number": "164000", "name": "قروض من البنوك والمؤسسات المالية", "root_type": "Liability", "parent": "16"},
	{"number": "165000", "name": "تأمينات وضمانات مستلمة", "root_type": "Liability", "parent": "16"},
	{"number": "519000", "name": "تسهيلات بنكية / سحب على المكشوف", "root_type": "Liability", "parent": "16"},
	{"number": "40", "name": "الموردون والحسابات المرتبطة", "root_type": "Liability", "parent": None, "is_group": 1},
	{"number": "401100", "name": "الموردون - مشتريات عامة وخدمات", "root_type": "Liability", "parent": "40", "account_type": "Payable"},
	{"number": "401110", "name": "موردو الأدوية البيطرية", "root_type": "Liability", "parent": "40", "account_type": "Payable"},
	{"number": "401120", "name": "موردو اللقاحات والمواد الحيوية", "root_type": "Liability", "parent": "40", "account_type": "Payable"},
	{"number": "401130", "name": "موردو أغذية الحيوانات والمكملات", "root_type": "Liability", "parent": "40", "account_type": "Payable"},
	{"number": "401140", "name": "موردو الإكسسوارات ومنتجات النظافة", "root_type": "Liability", "parent": "40", "account_type": "Payable"},
	{"number": "401150", "name": "موردو المصاريف والخدمات العامة", "root_type": "Liability", "parent": "40", "account_type": "Payable"},
	{"number": "401160", "name": "موردون خارجيون / استيراد", "root_type": "Liability", "parent": "40", "account_type": "Payable"},
	{"number": "403000", "name": "الموردون - أوراق دفع", "root_type": "Liability", "parent": "40", "account_type": "Payable"},
	{"number": "404100", "name": "موردو الأصول الثابتة", "root_type": "Liability", "parent": "40", "account_type": "Payable"},
	{"number": "408100", "name": "موردون - فواتير مخزون لم تصل بعد", "root_type": "Liability", "parent": "40", "account_type": "Stock Received But Not Billed"},
	{"number": "408400", "name": "موردو أصول ثابتة - فواتير لم تصل بعد", "root_type": "Liability", "parent": "40", "account_type": "Asset Received But Not Billed"},
	{"number": "408800", "name": "خدمات مستلمة ولم تفوتر بعد", "root_type": "Liability", "parent": "40", "account_type": "Service Received But Not Billed"},
	{"number": "419", "name": "العملاء الدائنون / مقدمات العملاء", "root_type": "Liability", "parent": None, "is_group": 1},
	{"number": "419100", "name": "مقدمات ودفعات مستلمة من العملاء", "root_type": "Liability", "parent": "419", "account_type": "Receivable"},
	{"number": "419700", "name": "إشعارات دائن للعملاء لم تصدر بعد", "root_type": "Liability", "parent": "419"},
	{"number": "42", "name": "الموظفون والحسابات المرتبطة", "root_type": "Liability", "parent": None, "is_group": 1},
	{"number": "421000", "name": "رواتب مستحقة للموظفين", "root_type": "Liability", "parent": "42"},
	{"number": "428200", "name": "مخصص إجازات مستحقة", "root_type": "Liability", "parent": "42"},
	{"number": "428600", "name": "مستحقات أخرى للموظفين", "root_type": "Liability", "parent": "42"},
	{"number": "43", "name": "الضمان الاجتماعي والجهات الاجتماعية", "root_type": "Liability", "parent": None, "is_group": 1},
	{"number": "431000", "name": "الضمان الاجتماعي", "root_type": "Liability", "parent": "43"},
	{"number": "437000", "name": "جهات اجتماعية أخرى", "root_type": "Liability", "parent": "43"},
	{"number": "438600", "name": "مصاريف اجتماعية مستحقة", "root_type": "Liability", "parent": "43"},
	{"number": "44", "name": "الدولة والضرائب المستحقة", "root_type": "Liability", "parent": None, "is_group": 1},
	{"number": "442100", "name": "استقطاعات / ضرائب مقتطعة من المصدر", "root_type": "Liability", "parent": "44", "account_type": "Tax"},
	{"number": "444000", "name": "ضريبة أرباح مستحقة", "root_type": "Liability", "parent": "44", "account_type": "Tax"},
	{"number": "445200", "name": "ضريبة قيمة مضافة مستحقة على عمليات خارجية", "root_type": "Liability", "parent": "44", "account_type": "Tax"},
	{"number": "445510", "name": "ضريبة قيمة مضافة واجبة الدفع", "root_type": "Liability", "parent": "44", "account_type": "Tax"},
	{"number": "445710", "name": "ضريبة قيمة مضافة محصلة - النسبة العادية", "root_type": "Liability", "parent": "44", "account_type": "Tax"},
	{"number": "445720", "name": "ضريبة قيمة مضافة محصلة - النسبة المخفضة", "root_type": "Liability", "parent": "44", "account_type": "Tax"},
	{"number": "445730", "name": "ضريبة قيمة مضافة محصلة - نسب أخرى", "root_type": "Liability", "parent": "44", "account_type": "Tax"},
	{"number": "45/46", "name": "الشركاء والدائنون الآخرون", "root_type": "Liability", "parent": None, "is_group": 1},
	{"number": "455000", "name": "حسابات جارية للشركاء", "root_type": "Liability", "parent": "45/46"},
	{"number": "467000", "name": "دائنون آخرون", "root_type": "Liability", "parent": "45/46"},
	{"number": "48", "name": "حسابات التسوية - جانب الالتزامات", "root_type": "Liability", "parent": None, "is_group": 1},
	{"number": "487000", "name": "إيرادات مقدمة / إيرادات مستلمة مقدماً", "root_type": "Liability", "parent": "48"},
	# Asset / الأصول
	{"number": "20", "name": "الأصول غير الملموسة", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "205000", "name": "برامج، تراخيص، موقع إلكتروني، تخصيص ERPNext", "root_type": "Asset", "parent": "20", "account_type": "Fixed Asset"},
	{"number": "208000", "name": "أصول غير ملموسة أخرى", "root_type": "Asset", "parent": "20", "account_type": "Fixed Asset"},
	{"number": "21", "name": "الأصول الثابتة الملموسة", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "215400", "name": "معدات بيطرية / معدات عيادة", "root_type": "Asset", "parent": "21", "account_type": "Fixed Asset"},
	{"number": "218100", "name": "تجهيزات المحل / العيادة", "root_type": "Asset", "parent": "21", "account_type": "Fixed Asset"},
	{"number": "218200", "name": "سيارات أو مركبات توصيل", "root_type": "Asset", "parent": "21", "account_type": "Fixed Asset"},
	{"number": "218300", "name": "أجهزة كمبيوتر / POS / طابعات", "root_type": "Asset", "parent": "21", "account_type": "Fixed Asset"},
	{"number": "218400", "name": "أثاث المحل والمكتب", "root_type": "Asset", "parent": "21", "account_type": "Fixed Asset"},
	{"number": "218500", "name": "ثلاجات ومعدات تبريد اللقاحات", "root_type": "Asset", "parent": "21", "account_type": "Fixed Asset"},
	{"number": "23", "name": "أصول ثابتة تحت التنفيذ", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "231000", "name": "أصول ثابتة ملموسة تحت التنفيذ", "root_type": "Asset", "parent": "23", "account_type": "Capital Work in Progress"},
	{"number": "28", "name": "مجمع إهلاك الأصول الثابتة", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "280500", "name": "مجمع إهلاك البرامج والتراخيص", "root_type": "Asset", "parent": "28", "account_type": "Accumulated Depreciation"},
	{"number": "281540", "name": "مجمع إهلاك المعدات البيطرية / العيادة", "root_type": "Asset", "parent": "28", "account_type": "Accumulated Depreciation"},
	{"number": "281830", "name": "مجمع إهلاك أجهزة الكمبيوتر و POS", "root_type": "Asset", "parent": "28", "account_type": "Accumulated Depreciation"},
	{"number": "281840", "name": "مجمع إهلاك الأثاث والتجهيزات", "root_type": "Asset", "parent": "28", "account_type": "Accumulated Depreciation"},
	{"number": "281800", "name": "مجمع إهلاك الأصول الثابتة - افتراضي", "root_type": "Asset", "parent": "28", "account_type": "Accumulated Depreciation"},
	{"number": "37", "name": "مخزون البضائع", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "370000", "name": "مخزون بضائع - المخزن الرئيسي", "root_type": "Asset", "parent": "37", "account_type": "Stock"},
	{"number": "371000", "name": "مخزون الأدوية البيطرية", "root_type": "Asset", "parent": "37", "account_type": "Stock"},
	{"number": "372000", "name": "مخزون اللقاحات / سلسلة التبريد", "root_type": "Asset", "parent": "37", "account_type": "Stock"},
	{"number": "373000", "name": "مخزون أغذية الحيوانات والمكملات", "root_type": "Asset", "parent": "37", "account_type": "Stock"},
	{"number": "374000", "name": "مخزون الإكسسوارات ومنتجات النظافة", "root_type": "Asset", "parent": "37", "account_type": "Stock"},
	{"number": "375000", "name": "مخزون مستهلكات العيادة والمختبر", "root_type": "Asset", "parent": "37", "account_type": "Stock"},
	{"number": "377000", "name": "مخزون المنتجات المقيدة أو المنظمة", "root_type": "Asset", "parent": "37", "account_type": "Stock"},
	{"number": "380000", "name": "مخزون في الطريق / بضاعة أمانة", "root_type": "Asset", "parent": "37", "account_type": "Stock"},
	{"number": "397000", "name": "مخصص انخفاض قيمة المخزون", "root_type": "Asset", "parent": "37"},
	{"number": "409", "name": "الموردون المدينون", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "409100", "name": "مقدمات ودفعات مدفوعة للموردين", "root_type": "Asset", "parent": "409", "account_type": "Payable"},
	{"number": "409700", "name": "إشعارات دائن مستحقة من الموردين", "root_type": "Asset", "parent": "409"},
	{"number": "411", "name": "العملاء والحسابات المرتبطة", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "411100", "name": "العملاء - مبيعات بضائع وخدمات", "root_type": "Asset", "parent": "411", "account_type": "Receivable"},
	{"number": "411110", "name": "عملاء الكاشير / POS آجل", "root_type": "Asset", "parent": "411", "account_type": "Receivable"},
	{"number": "411120", "name": "عملاء شركات / مزارع / عيادات", "root_type": "Asset", "parent": "411", "account_type": "Receivable"},
	{"number": "416000", "name": "عملاء مشكوك في تحصيلهم أو محل نزاع", "root_type": "Asset", "parent": "411", "account_type": "Receivable"},
	{"number": "418100", "name": "عملاء - فواتير لم تصدر بعد", "root_type": "Asset", "parent": "411"},
	{"number": "425", "name": "الموظفون المدينون", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "425000", "name": "سلف ومقدمات للموظفين", "root_type": "Asset", "parent": "425"},
	{"number": "445", "name": "الدولة - ضريبة قابلة للاسترداد", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "445620", "name": "ضريبة قيمة مضافة قابلة للخصم على الأصول الثابتة", "root_type": "Asset", "parent": "445", "account_type": "Tax"},
	{"number": "445660", "name": "ضريبة قيمة مضافة قابلة للخصم على البضائع والخدمات", "root_type": "Asset", "parent": "445", "account_type": "Tax"},
	{"number": "445670", "name": "رصيد ضريبة قيمة مضافة مرحل / قابل للاسترداد", "root_type": "Asset", "parent": "445", "account_type": "Tax"},
	{"number": "486", "name": "حسابات التسوية - جانب الأصول", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "486000", "name": "مصاريف مدفوعة مقدماً", "root_type": "Asset", "parent": "486"},
	{"number": "51/53", "name": "النقدية والبنوك", "root_type": "Asset", "parent": None, "is_group": 1},
	{"number": "511200", "name": "شيكات تحت التحصيل", "root_type": "Asset", "parent": "51/53", "account_type": "Bank"},
	{"number": "511500", "name": "بطاقات بنكية تحت التحصيل", "root_type": "Asset", "parent": "51/53", "account_type": "Bank"},
	{"number": "511700", "name": "محافظ إلكترونية / Mobile Money تحت التحصيل", "root_type": "Asset", "parent": "51/53", "account_type": "Bank"},
	{"number": "512101", "name": "البنك الرئيسي", "root_type": "Asset", "parent": "51/53", "account_type": "Bank"},
	{"number": "512102", "name": "بنك ثانوي", "root_type": "Asset", "parent": "51/53", "account_type": "Bank"},
	{"number": "517000", "name": "جهات مالية أخرى / محافظ إلكترونية", "root_type": "Asset", "parent": "51/53", "account_type": "Bank"},
	{"number": "531000", "name": "الصندوق الرئيسي", "root_type": "Asset", "parent": "51/53", "is_group": 1, "account_type": "Cash"},
	{"number": "531101", "name": "صندوق كاشير POS 1 - المحل", "root_type": "Asset", "parent": "531000", "account_type": "Cash"},
	{"number": "531102", "name": "صندوق كاشير POS 2 - العيادة", "root_type": "Asset", "parent": "531000", "account_type": "Cash"},
	{"number": "531900", "name": "صندوق مصاريف صغيرة", "root_type": "Asset", "parent": "531000", "account_type": "Cash"},
	{"number": "580000", "name": "تحويلات داخلية بين الصندوق والبنك", "root_type": "Asset", "parent": "51/53"},
	{"number": "590000", "name": "مخصص انخفاض الحسابات المالية", "root_type": "Asset", "parent": "51/53"},
	# Expense / المصاريف
	{"number": "60", "name": "المشتريات وتغير المخزون", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "603700", "name": "تكلفة البضاعة المباعة / تغير مخزون البضائع", "root_type": "Expense", "parent": "60", "account_type": "Cost of Goods Sold"},
	{"number": "603790", "name": "تسويات المخزون / فروقات الجرد", "root_type": "Expense", "parent": "60", "account_type": "Stock Adjustment"},
	{"number": "607000", "name": "مشتريات بضائع - افتراضي", "root_type": "Expense", "parent": "60", "account_type": "Expense Account"},
	{"number": "607100", "name": "مشتريات أدوية بيطرية", "root_type": "Expense", "parent": "60", "account_type": "Expense Account"},
	{"number": "607200", "name": "مشتريات لقاحات ومواد حيوية", "root_type": "Expense", "parent": "60", "account_type": "Expense Account"},
	{"number": "607300", "name": "مشتريات أغذية حيوانات ومكملات", "root_type": "Expense", "parent": "60", "account_type": "Expense Account"},
	{"number": "607400", "name": "مشتريات إكسسوارات ومنتجات نظافة", "root_type": "Expense", "parent": "60", "account_type": "Expense Account"},
	{"number": "607500", "name": "مشتريات مستهلكات عيادة ومختبر", "root_type": "Expense", "parent": "60", "account_type": "Expense Account"},
	{"number": "607600", "name": "مشتريات منتجات مقيدة أو منظمة", "root_type": "Expense", "parent": "60", "account_type": "Expense Account"},
	{"number": "608000", "name": "مصاريف إضافية محملة على المشتريات", "root_type": "Expense", "parent": "60", "account_type": "Expense Account"},
	{"number": "609000", "name": "خصومات ومردودات مكتسبة من الموردين", "root_type": "Expense", "parent": "60", "account_type": "Expense Account"},
	{"number": "61", "name": "خدمات خارجية", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "611000", "name": "خدمات خارجية / تعهيد عام", "root_type": "Expense", "parent": "61", "account_type": "Expense Account"},
	{"number": "613200", "name": "إيجار المحل / العيادة", "root_type": "Expense", "parent": "61", "account_type": "Expense Account"},
	{"number": "614000", "name": "مصاريف إيجار وخدمات عقارية", "root_type": "Expense", "parent": "61", "account_type": "Expense Account"},
	{"number": "615200", "name": "صيانة المحل والمباني", "root_type": "Expense", "parent": "61", "account_type": "Expense Account"},
	{"number": "615500", "name": "صيانة المعدات", "root_type": "Expense", "parent": "61", "account_type": "Expense Account"},
	{"number": "615600", "name": "صيانة ERPNext / POS / الأجهزة", "root_type": "Expense", "parent": "61", "account_type": "Expense Account"},
	{"number": "616000", "name": "التأمينات", "root_type": "Expense", "parent": "61", "account_type": "Expense Account"},
	{"number": "617000", "name": "دراسات وأبحاث", "root_type": "Expense", "parent": "61", "account_type": "Expense Account"},
	{"number": "618300", "name": "مراجع ووثائق تقنية / بيطرية", "root_type": "Expense", "parent": "61", "account_type": "Expense Account"},
	{"number": "62", "name": "خدمات خارجية أخرى", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "622600", "name": "أتعاب محاسب / طبيب بيطري خارجي / استشارات", "root_type": "Expense", "parent": "62", "account_type": "Expense Account"},
	{"number": "623000", "name": "إعلانات وتسويق", "root_type": "Expense", "parent": "62", "account_type": "Expense Account"},
	{"number": "624100", "name": "نقل وشحن على المشتريات", "root_type": "Expense", "parent": "62", "account_type": "Chargeable"},
	{"number": "624200", "name": "توصيل وشحن على المبيعات", "root_type": "Expense", "parent": "62", "account_type": "Chargeable"},
	{"number": "625100", "name": "سفر وتنقلات", "root_type": "Expense", "parent": "62", "account_type": "Expense Account"},
	{"number": "625700", "name": "ضيافة واستقبال", "root_type": "Expense", "parent": "62", "account_type": "Expense Account"},
	{"number": "626000", "name": "هاتف وإنترنت", "root_type": "Expense", "parent": "62", "account_type": "Expense Account"},
	{"number": "627000", "name": "مصاريف بنكية", "root_type": "Expense", "parent": "62", "account_type": "Expense Account"},
	{"number": "628100", "name": "اشتراكات وجمعيات مهنية", "root_type": "Expense", "parent": "62", "account_type": "Expense Account"},
	{"number": "628400", "name": "مصاريف توظيف", "root_type": "Expense", "parent": "62", "account_type": "Expense Account"},
	{"number": "63", "name": "ضرائب ورسوم غير مباشرة", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "635000", "name": "ضرائب محلية / ضرائب غير قابلة للاسترداد", "root_type": "Expense", "parent": "63", "account_type": "Expense Account"},
	{"number": "64", "name": "مصاريف الموظفين", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "641100", "name": "رواتب البائعين والكاشير", "root_type": "Expense", "parent": "64", "account_type": "Expense Account"},
	{"number": "641200", "name": "رواتب الأطباء البيطريين وطاقم العيادة", "root_type": "Expense", "parent": "64", "account_type": "Expense Account"},
	{"number": "641300", "name": "مكافآت وحوافز", "root_type": "Expense", "parent": "64", "account_type": "Expense Account"},
	{"number": "645000", "name": "مساهمات اجتماعية على صاحب العمل", "root_type": "Expense", "parent": "64", "account_type": "Expense Account"},
	{"number": "647000", "name": "مصاريف اجتماعية أخرى", "root_type": "Expense", "parent": "64", "account_type": "Expense Account"},
	{"number": "65", "name": "مصاريف تشغيلية أخرى", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "654000", "name": "ديون معدومة / شطب ذمم العملاء", "root_type": "Expense", "parent": "65", "account_type": "Expense Account"},
	{"number": "658000", "name": "فروقات صندوق / فروقات تسوية", "root_type": "Expense", "parent": "65", "account_type": "Expense Account"},
	{"number": "658100", "name": "فروقات تقريب الفواتير", "root_type": "Expense", "parent": "65", "account_type": "Round Off"},
	{"number": "66", "name": "مصاريف مالية", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "661000", "name": "فوائد قروض", "root_type": "Expense", "parent": "66", "account_type": "Expense Account"},
	{"number": "666000", "name": "أرباح وخسائر فروقات العملة - حساب ERPNext موحد", "root_type": "Expense", "parent": "66", "account_type": "Expense Account"},
	{"number": "666100", "name": "فروقات عملة غير محققة", "root_type": "Expense", "parent": "66", "account_type": "Expense Account"},
	{"number": "67", "name": "مصاريف استثنائية", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "671000", "name": "مصاريف استثنائية", "root_type": "Expense", "parent": "67", "account_type": "Expense Account"},
	{"number": "675775", "name": "ربح / خسارة بيع أو التخلص من أصل ثابت", "root_type": "Expense", "parent": "67", "account_type": "Expense Account"},
	{"number": "68", "name": "مخصصات وإهلاكات", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "681100", "name": "مصروف إهلاك الأصول الثابتة", "root_type": "Expense", "parent": "68", "account_type": "Depreciation"},
	{"number": "681730", "name": "مخصص انخفاض قيمة المخزون", "root_type": "Expense", "parent": "68", "account_type": "Expense Account"},
	{"number": "69", "name": "ضريبة الأرباح", "root_type": "Expense", "parent": None, "is_group": 1},
	{"number": "695000", "name": "مصروف ضريبة الأرباح", "root_type": "Expense", "parent": "69", "account_type": "Expense Account"},
	# Income / الإيرادات
	{"number": "70", "name": "مبيعات البضائع والخدمات", "root_type": "Income", "parent": None, "is_group": 1},
	{"number": "706100", "name": "إيرادات استشارات بيطرية", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "706200", "name": "إيرادات علاجات وخدمات بيطرية", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "706300", "name": "إيرادات تحاليل مختبر وخدمات عيادة", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "706400", "name": "إيرادات عناية بالحيوانات / Grooming / Boarding إن وجدت", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "707000", "name": "مبيعات بضائع - افتراضي", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "707100", "name": "مبيعات أدوية بيطرية", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "707200", "name": "مبيعات لقاحات ومواد حيوية", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "707300", "name": "مبيعات أغذية حيوانات ومكملات", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "707400", "name": "مبيعات إكسسوارات ومنتجات نظافة", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "707500", "name": "مبيعات مستهلكات عيادة ومختبر", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "707600", "name": "مبيعات منتجات مقيدة أو منظمة", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "708500", "name": "إيرادات شحن وتوصيل مفوترة للعميل", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "709600", "name": "خصومات ممنوحة على الخدمات", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "709700", "name": "خصومات ممنوحة على البضائع", "root_type": "Income", "parent": "70", "account_type": "Income Account"},
	{"number": "74", "name": "إعانات ومنح تشغيلية", "root_type": "Income", "parent": None, "is_group": 1},
	{"number": "741000", "name": "إعانات تشغيلية", "root_type": "Income", "parent": "74", "account_type": "Income Account"},
	{"number": "75", "name": "إيرادات تشغيلية أخرى", "root_type": "Income", "parent": None, "is_group": 1},
	{"number": "758000", "name": "فروقات صندوق موجبة / إيرادات متنوعة", "root_type": "Income", "parent": "75", "account_type": "Income Account"},
	{"number": "76", "name": "إيرادات مالية", "root_type": "Income", "parent": None, "is_group": 1},
	{"number": "766000", "name": "أرباح فروقات عملة، إذا تم فصلها", "root_type": "Income", "parent": "76", "account_type": "Income Account"},
	{"number": "77", "name": "إيرادات استثنائية", "root_type": "Income", "parent": None, "is_group": 1},
	{"number": "775000", "name": "إيرادات بيع أصول ثابتة", "root_type": "Income", "parent": "77", "account_type": "Income Account"},
)

COMPANY_ACCOUNT_DEFAULTS: dict[str, str] = {
	"default_bank_account": "512101",
	"default_cash_account": "531101",
	"default_receivable_account": "411100",
	"default_payable_account": "401100",
	"default_income_account": "707000",
	"default_expense_account": "603700",
	"purchase_expense_account": "607000",
	"service_expense_account": "611000",
	"default_discount_account": "709700",
	"write_off_account": "654000",
	"round_off_account": "658100",
	"exchange_gain_loss_account": "666000",
	"unrealized_exchange_gain_loss_account": "666100",
	"default_deferred_revenue_account": "487000",
	"default_deferred_expense_account": "486000",
	"default_advance_received_account": "419100",
	"default_advance_paid_account": "409100",
	"accumulated_depreciation_account": "281800",
	"depreciation_expense_account": "681100",
	"disposal_account": "675775",
	"capital_work_in_progress_account": "231000",
	"asset_received_but_not_billed": "408400",
	"default_inventory_account": "370000",
	"stock_adjustment_account": "603790",
	"stock_received_but_not_billed": "408100",
	"default_provisional_account": "408800",
}

COMPANY_COST_CENTER_DEFAULTS = {
	"cost_center": "الإدارة",
	"round_off_cost_center": "المبيعات / المحل",
	"depreciation_cost_center": "الإدارة",
}

MOP_ROWS = (
	("نقداً", "Cash", "531101"),
	("بطاقة بنكية", "Bank", "511500"),
	("تحويل بنكي", "Bank", "512101"),
	("شيك", "Bank", "511200"),
	("محفظة إلكترونية / Mobile Money", "Bank", "511700"),
)

SUPPLIER_GROUPS = (
	"موردو الأدوية البيطرية",
	"موردو اللقاحات",
	"موردو أغذية الحيوانات",
	"موردو الإكسسوارات والنظافة",
	"موردو الخدمات والمصاريف العامة",
)

ITEM_GROUP_ROWS = (
	("أدوية بيطرية", "707100", "603700", "371000", "المخزن الرئيسي"),
	("لقاحات ومواد حيوية", "707200", "603700", "372000", "ثلاجة اللقاحات"),
	("أغذية حيوانات ومكملات", "707300", "603700", "373000", "المخزن الرئيسي"),
	("إكسسوارات ومنتجات نظافة", "707400", "603700", "374000", "المخزن الرئيسي"),
	("مستهلكات عيادة ومختبر", "707500", "603700", "375000", "رفوف الصيدلية"),
	("خدمات بيطرية", "706100", "611000", None, None),
	("خدمة توصيل", "708500", "624200", None, None),
)

VAT_TEMPLATE_ROWS = (
	("Sales Taxes and Charges Template", "ضريبة مبيعات عادية", "445710"),
	("Sales Taxes and Charges Template", "ضريبة مبيعات مخفضة", "445720"),
	("Purchase Taxes and Charges Template", "ضريبة مشتريات قابلة للخصم", "445660"),
	("Purchase Taxes and Charges Template", "ضريبة مشتريات أصول ثابتة قابلة للخصم", "445620"),
)

ACCOUNT_TYPE_VALIDATION = {
	"531101": "Cash",
	"512101": "Bank",
	"411100": "Receivable",
	"401100": "Payable",
	"370000": "Stock",
	"408100": "Stock Received But Not Billed",
	"408400": "Asset Received But Not Billed",
	"408800": "Service Received But Not Billed",
	"603790": "Stock Adjustment",
	"681100": "Depreciation",
}


def _new_summary(company: str) -> dict[str, Any]:
	return {"company": company, "created": [], "updated": [], "skipped": [], "warnings": [], "errors": []}


def _summary_append(bucket: str, message: str):
	if _ACTIVE_SUMMARY is not None:
		_ACTIVE_SUMMARY[bucket].append(message)


def _created(message: str):
	_summary_append("created", message)
	LOGGER.info("created: %s", message)


def _updated(message: str):
	_summary_append("updated", message)
	LOGGER.info("updated: %s", message)


def _skipped(message: str):
	_summary_append("skipped", message)
	LOGGER.info("skipped: %s", message)


def _warn(message: str):
	_summary_append("warnings", message)
	LOGGER.warning(message)


def _error(message: str, exc: Exception | None = None):
	_summary_append("errors", message)
	if exc:
		LOGGER.error("%s: %s", message, exc, exc_info=True)
	else:
		LOGGER.error(message)


def _meta(doctype: str):
	if doctype not in _META_CACHE:
		_META_CACHE[doctype] = frappe.get_meta(doctype)
	return _META_CACHE[doctype]


def _has_field(doctype: str, fieldname: str) -> bool:
	return bool(_meta(doctype).has_field(fieldname))


def _set_if_has_field(doc, doctype: str, fieldname: str, value: Any, changed: list[str] | None = None) -> bool:
	if not _has_field(doctype, fieldname):
		return False
	if doc.get(fieldname) == value:
		return False
	doc.set(fieldname, value)
	if changed is not None:
		changed.append(fieldname)
	return True


def _set_child_if_has_field(row, child_doctype: str, fieldname: str, value: Any, changed: list[str] | None = None) -> bool:
	if not _has_field(child_doctype, fieldname):
		return False
	if row.get(fieldname) == value:
		return False
	row.set(fieldname, value)
	if changed is not None:
		changed.append(fieldname)
	return True


def _prime_required_metadata():
	for doctype in REQUIRED_META_DOCTYPES:
		_meta(doctype)


def _doctype_exists(doctype: str) -> bool:
	return bool(frappe.db.exists("DocType", doctype))


def _table_field(parent_doctype: str, required_child_fields: tuple[str, ...], preferred_options: tuple[str, ...] = ()) -> tuple[str | None, str | None]:
	parent_meta = _meta(parent_doctype)
	table_fields = [df for df in parent_meta.fields if df.fieldtype == "Table" and df.options]

	for option in preferred_options:
		for df in table_fields:
			if df.options != option:
				continue
			child_meta = _meta(df.options)
			if all(child_meta.has_field(fieldname) for fieldname in required_child_fields):
				return df.fieldname, df.options

	for df in table_fields:
		child_meta = _meta(df.options)
		if all(child_meta.has_field(fieldname) for fieldname in required_child_fields):
			return df.fieldname, df.options

	return None, None


def _first_root_node(doctype: str, parent_field: str) -> str | None:
	rows = frappe.get_all(
		doctype,
		fields=["name", parent_field, "is_group", "lft"],
		order_by="lft asc, name asc",
		limit=200,
	)
	for row in rows:
		if row.get("is_group") and not row.get(parent_field):
			return row.name
	for row in rows:
		if row.get("is_group"):
			return row.name
	return None


def _save_doc(doc, label: str):
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	_updated(label)


def _insert_doc(doc, label: str | None = None):
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	_created(label or f"{doc.doctype} {doc.name}")
	return doc


def get_company_abbr(company: str) -> str:
	"""Return the ERPNext abbreviation for an existing Company."""
	if company not in _COMPANY_ABBR_CACHE:
		abbr = frappe.db.get_value("Company", company, "abbr")
		if not abbr:
			frappe.throw(f"Company '{company}' does not exist or has no abbreviation.")
		_COMPANY_ABBR_CACHE[company] = abbr
	return _COMPANY_ABBR_CACHE[company]


def get_root_account(company: str, root_type: str) -> str:
	"""Find the ERPNext root Account for a company and root type."""
	if root_type not in VALID_ROOT_TYPES:
		frappe.throw(f"Invalid ERPNext Account root_type '{root_type}'.")

	rows = frappe.get_all(
		"Account",
		filters={"company": company, "root_type": root_type, "is_group": 1},
		fields=["name", "parent_account", "lft"],
		order_by="lft asc, name asc",
		limit=50,
	)
	for row in rows:
		if not row.parent_account:
			return row.name
	if rows:
		_warn(f"No root Account without parent found for {root_type}; using first group account {rows[0].name}.")
		return rows[0].name
	frappe.throw(f"No root Account found for company '{company}' and root_type '{root_type}'.")


def _account_rows_by_number(company: str, account_number: str, root_type: str | None = None) -> list[dict[str, Any]]:
	if not _has_field("Account", "account_number"):
		return []

	filters: dict[str, Any] = {"company": company, "account_number": str(account_number)}
	if root_type:
		filters["root_type"] = root_type

	return frappe.get_all(
		"Account",
		filters=filters,
		fields=["name", "account_name", "account_number", "account_type", "root_type", "parent_account", "is_group", "lft"],
		order_by="lft asc, name asc",
		limit=20,
	)


def get_account_by_number(company: str, account_number: str) -> str | None:
	"""Return the Account name for a company/account_number pair."""
	rows = _account_rows_by_number(company, account_number)
	if not rows:
		return None
	if len(rows) > 1:
		_warn(f"Multiple Accounts found for account_number {account_number}; using {rows[0].name}.")
	return rows[0].name


def _get_account_by_number_and_root(company: str, account_number: str, root_type: str) -> str | None:
	rows = _account_rows_by_number(company, account_number, root_type=root_type)
	if not rows:
		return None
	if len(rows) > 1:
		_warn(f"Multiple {root_type} Accounts found for account_number {account_number}; using {rows[0].name}.")
	return rows[0].name


def get_account_name(company: str, account_number: str) -> str | None:
	"""Return the actual ERPNext Account document name for a PCG number."""
	return get_account_by_number(company, account_number)


def _account_has_children(account: str) -> bool:
	return bool(frappe.db.exists("Account", {"parent_account": account}))


def _account_has_gl_entries(account: str) -> bool:
	if not _doctype_exists("GL Entry"):
		return False
	return bool(frappe.db.exists("GL Entry", {"account": account, "is_cancelled": 0}))


def _safe_to_retype_or_move_account(account: str) -> bool:
	return not _account_has_children(account) and not _account_has_gl_entries(account)


def _legacy_account_match(company: str, arabic_name: str, root_type: str, parent_account: str | None) -> str | None:
	filters = {"company": company, "account_name": arabic_name, "root_type": root_type}
	if parent_account:
		filters["parent_account"] = parent_account
	return frappe.db.get_value("Account", filters, "name")


def ensure_account(
	company: str,
	number: str,
	arabic_name: str,
	root_type: str,
	parent_number: str | None = None,
	is_group: int = 0,
	account_type: str | None = None,
) -> str | None:
	"""Create or safely update one Account using account_number-based lookup."""
	_meta("Account")
	parent_account = get_root_account(company, root_type) if not parent_number else _get_account_by_number_and_root(company, parent_number, root_type)
	if parent_number and not parent_account:
		_error(f"Parent Account number {parent_number} was not found for {number} {arabic_name}.")
		return None

	account = _get_account_by_number_and_root(company, number, root_type)
	if not account:
		other_root_account = get_account_by_number(company, number)
		if other_root_account:
			_error(
				f"Account number {number} already exists as {other_root_account} outside root_type {root_type}; cannot create duplicate in ERPNext."
			)
			return other_root_account

	account = account or _legacy_account_match(company, arabic_name, root_type, parent_account)

	if account:
		doc = frappe.get_doc("Account", account)
		changed: list[str] = []
		safe = _safe_to_retype_or_move_account(doc.name)

		if _has_field("Account", "account_number") and not doc.get("account_number"):
			_set_if_has_field(doc, "Account", "account_number", str(number), changed)

		if doc.get("account_name") != arabic_name:
			if safe:
				_set_if_has_field(doc, "Account", "account_name", arabic_name, changed)
			else:
				_warn(f"Skipped Account name update for used/non-leaf account {doc.name}.")

		if parent_account and doc.get("parent_account") != parent_account:
			if safe:
				_set_if_has_field(doc, "Account", "parent_account", parent_account, changed)
			else:
				_warn(f"Skipped Account parent update for used/non-leaf account {doc.name}.")

		if int(doc.get("is_group") or 0) != int(is_group or 0):
			if safe:
				_set_if_has_field(doc, "Account", "is_group", int(is_group or 0), changed)
			else:
				_warn(f"Skipped Account is_group update for used/non-leaf account {doc.name}.")

		if account_type and doc.get("account_type") != account_type:
			if not doc.get("account_type") or safe:
				_set_if_has_field(doc, "Account", "account_type", account_type, changed)
			else:
				_warn(f"Skipped Account type update for used account {doc.name}.")

		if changed:
			doc.flags.ignore_permissions = True
			doc.flags.ignore_root_company_validation = True
			doc.save(ignore_permissions=True)
			_updated(f"Account {doc.name}: {', '.join(changed)}")
		else:
			_skipped(f"Account {doc.name}")
		return doc.name

	doc = frappe.new_doc("Account")
	_set_if_has_field(doc, "Account", "account_name", arabic_name)
	_set_if_has_field(doc, "Account", "account_number", str(number))
	_set_if_has_field(doc, "Account", "company", company)
	_set_if_has_field(doc, "Account", "parent_account", parent_account)
	_set_if_has_field(doc, "Account", "is_group", int(is_group or 0))
	_set_if_has_field(doc, "Account", "root_type", root_type)
	if account_type:
		_set_if_has_field(doc, "Account", "account_type", account_type)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_root_company_validation = True
	doc.insert(ignore_permissions=True)
	_created(f"Account {doc.name}")
	return doc.name


def ensure_company_default(company: str, fieldname: str, account_number: str):
	"""Set a Company default account field if the field exists."""
	_meta("Company")
	if not _has_field("Company", fieldname):
		_skipped(f"Company.{fieldname} is not available")
		return

	account = get_account_name(company, account_number)
	if not account:
		_error(f"Cannot set Company.{fieldname}; account_number {account_number} was not found.")
		return

	doc = frappe.get_doc("Company", company)
	if doc.get(fieldname) == account:
		_skipped(f"Company.{fieldname}")
		return
	doc.set(fieldname, account)
	_save_doc(doc, f"Company.{fieldname} = {account}")


def _ensure_company_value(company: str, fieldname: str, value: Any):
	_meta("Company")
	if not _has_field("Company", fieldname):
		_skipped(f"Company.{fieldname} is not available")
		return
	doc = frappe.get_doc("Company", company)
	if doc.get(fieldname) == value:
		_skipped(f"Company.{fieldname}")
		return
	doc.set(fieldname, value)
	_save_doc(doc, f"Company.{fieldname} = {value}")


def _cost_center_by_label(company: str, name: str) -> str | None:
	return (
		frappe.db.get_value("Cost Center", {"company": company, "cost_center_name": name}, "name")
		or frappe.db.get_value("Cost Center", {"company": company, "name": name}, "name")
	)


def _cost_center_has_children(cost_center: str) -> bool:
	return bool(frappe.db.exists("Cost Center", {"parent_cost_center": cost_center}))


def _cost_center_has_gl_entries(cost_center: str) -> bool:
	if not _doctype_exists("GL Entry"):
		return False
	return bool(frappe.db.exists("GL Entry", {"cost_center": cost_center, "is_cancelled": 0}))


def ensure_cost_center(company: str, name: str, parent: str | None = None, is_group: int = 0) -> str | None:
	"""Create or safely update a Cost Center."""
	_meta("Cost Center")
	parent_cost_center = None
	if parent:
		parent_cost_center = _cost_center_by_label(company, parent) or parent

	existing = _cost_center_by_label(company, name)
	if existing:
		doc = frappe.get_doc("Cost Center", existing)
		changed: list[str] = []
		safe = not _cost_center_has_children(doc.name) and not _cost_center_has_gl_entries(doc.name)
		if int(doc.get("is_group") or 0) != int(is_group or 0):
			if safe:
				_set_if_has_field(doc, "Cost Center", "is_group", int(is_group or 0), changed)
			else:
				_warn(f"Skipped Cost Center is_group update for used/non-leaf cost center {doc.name}.")
		if parent_cost_center and doc.get("parent_cost_center") != parent_cost_center:
			if safe:
				_set_if_has_field(doc, "Cost Center", "parent_cost_center", parent_cost_center, changed)
			else:
				_warn(f"Skipped Cost Center parent update for used/non-leaf cost center {doc.name}.")
		if changed:
			_save_doc(doc, f"Cost Center {doc.name}: {', '.join(changed)}")
		else:
			_skipped(f"Cost Center {doc.name}")
		return doc.name

	doc = frappe.new_doc("Cost Center")
	_set_if_has_field(doc, "Cost Center", "cost_center_name", name)
	_set_if_has_field(doc, "Cost Center", "company", company)
	_set_if_has_field(doc, "Cost Center", "is_group", int(is_group or 0))
	if parent_cost_center:
		_set_if_has_field(doc, "Cost Center", "parent_cost_center", parent_cost_center)
	_insert_doc(doc)
	return doc.name


def _warehouse_by_label(company: str, name: str) -> str | None:
	return (
		frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": name}, "name")
		or frappe.db.get_value("Warehouse", {"company": company, "name": name}, "name")
	)


def _warehouse_has_children(warehouse: str) -> bool:
	return bool(frappe.db.exists("Warehouse", {"parent_warehouse": warehouse}))


def _warehouse_has_stock_entries(warehouse: str) -> bool:
	if not _doctype_exists("Stock Ledger Entry"):
		return False
	return bool(frappe.db.exists("Stock Ledger Entry", {"warehouse": warehouse, "is_cancelled": 0}))


def ensure_warehouse(
	company: str,
	name: str,
	parent: str | None = None,
	is_group: int = 0,
	warehouse_type: str | None = None,
) -> str | None:
	"""Create or safely update a Warehouse."""
	_meta("Warehouse")
	parent_warehouse = None
	if parent:
		parent_warehouse = _warehouse_by_label(company, parent) or parent

	if warehouse_type and _has_field("Warehouse", "warehouse_type"):
		if not _doctype_exists("Warehouse Type") or not frappe.db.exists("Warehouse Type", warehouse_type):
			_warn(f"Warehouse Type {warehouse_type} is not available; not setting it on {name}.")
			warehouse_type = None

	existing = _warehouse_by_label(company, name)
	if existing:
		doc = frappe.get_doc("Warehouse", existing)
		changed: list[str] = []
		safe = not _warehouse_has_children(doc.name) and not _warehouse_has_stock_entries(doc.name)
		if int(doc.get("is_group") or 0) != int(is_group or 0):
			if safe:
				_set_if_has_field(doc, "Warehouse", "is_group", int(is_group or 0), changed)
			else:
				_warn(f"Skipped Warehouse is_group update for used/non-leaf warehouse {doc.name}.")
		if parent_warehouse and doc.get("parent_warehouse") != parent_warehouse:
			if safe:
				_set_if_has_field(doc, "Warehouse", "parent_warehouse", parent_warehouse, changed)
			else:
				_warn(f"Skipped Warehouse parent update for used/non-leaf warehouse {doc.name}.")
		if warehouse_type and doc.get("warehouse_type") != warehouse_type:
			if not _warehouse_has_stock_entries(doc.name):
				_set_if_has_field(doc, "Warehouse", "warehouse_type", warehouse_type, changed)
			else:
				_warn(f"Skipped Warehouse Type update for used warehouse {doc.name}.")
		if changed:
			_save_doc(doc, f"Warehouse {doc.name}: {', '.join(changed)}")
		else:
			_skipped(f"Warehouse {doc.name}")
		return doc.name

	doc = frappe.new_doc("Warehouse")
	_set_if_has_field(doc, "Warehouse", "warehouse_name", name)
	_set_if_has_field(doc, "Warehouse", "company", company)
	_set_if_has_field(doc, "Warehouse", "is_group", int(is_group or 0))
	if parent_warehouse:
		_set_if_has_field(doc, "Warehouse", "parent_warehouse", parent_warehouse)
	if warehouse_type:
		_set_if_has_field(doc, "Warehouse", "warehouse_type", warehouse_type)
	_insert_doc(doc)
	return doc.name


def ensure_mode_of_payment(name: str, mop_type: str, company: str, account_number: str):
	"""Create/update a Mode of Payment and its default account row."""
	_meta("Mode of Payment")
	account = get_account_name(company, account_number)
	if not account:
		_error(f"Cannot ensure Mode of Payment {name}; account_number {account_number} was not found.")
		return

	table_field, child_doctype = _table_field("Mode of Payment", ("company", "default_account"), ("Mode of Payment Account",))
	if not table_field or not child_doctype:
		_warn("Mode of Payment default account child table was not found.")
		return

	if frappe.db.exists("Mode of Payment", name):
		doc = frappe.get_doc("Mode of Payment", name)
		is_new = False
	else:
		doc = frappe.new_doc("Mode of Payment")
		_set_if_has_field(doc, "Mode of Payment", "mode_of_payment", name)
		is_new = True

	changed: list[str] = []
	_set_if_has_field(doc, "Mode of Payment", "type", mop_type, changed)
	_set_if_has_field(doc, "Mode of Payment", "enabled", 1, changed)

	rows = [row for row in doc.get(table_field) if row.get("company") == company]
	if rows:
		row = rows[0]
		if len(rows) > 1:
			_warn(f"Mode of Payment {name} has duplicate default account rows for {company}; first row updated only.")
	else:
		row = doc.append(table_field, {})
		changed.append(table_field)
	_set_child_if_has_field(row, child_doctype, "company", company, changed)
	_set_child_if_has_field(row, child_doctype, "default_account", account, changed)

	if is_new:
		_insert_doc(doc, f"Mode of Payment {name}")
	elif changed:
		_save_doc(doc, f"Mode of Payment {name}: {', '.join(sorted(set(changed)))}")
	else:
		_skipped(f"Mode of Payment {name}")


def _ensure_customer_group() -> str | None:
	_meta("Customer Group")
	if frappe.db.exists("Customer Group", "Individual"):
		return "Individual"
	name = "عملاء التجزئة"
	if frappe.db.exists("Customer Group", name):
		return name

	doc = frappe.new_doc("Customer Group")
	_set_if_has_field(doc, "Customer Group", "customer_group_name", name)
	_set_if_has_field(doc, "Customer Group", "is_group", 0)
	parent = _first_root_node("Customer Group", "parent_customer_group")
	if parent:
		_set_if_has_field(doc, "Customer Group", "parent_customer_group", parent)
	_insert_doc(doc, f"Customer Group {name}")
	return doc.name


def _territory() -> str | None:
	if not _doctype_exists("Territory"):
		return None
	if frappe.db.exists("Territory", "All Territories"):
		return "All Territories"
	row = frappe.get_all("Territory", fields=["name"], order_by="is_group desc, lft asc, name asc", limit=1)
	return row[0].name if row else None


def _party_account_table(parent_doctype: str) -> tuple[str | None, str | None]:
	return _table_field(parent_doctype, ("company", "account"), ("Party Account",))


def ensure_customer(company: str) -> str | None:
	"""Create or update the walk-in POS customer."""
	_meta("Customer")
	customer_group = _ensure_customer_group()
	territory = _territory()
	account = get_account_name(company, "411100")

	existing = frappe.db.get_value("Customer", {"customer_name": CUSTOMER_NAME}, "name")
	if existing:
		doc = frappe.get_doc("Customer", existing)
		is_new = False
	else:
		doc = frappe.new_doc("Customer")
		is_new = True

	changed: list[str] = []
	_set_if_has_field(doc, "Customer", "customer_name", CUSTOMER_NAME, changed)
	_set_if_has_field(doc, "Customer", "customer_type", "Individual", changed)
	if customer_group:
		_set_if_has_field(doc, "Customer", "customer_group", customer_group, changed)
	if territory:
		_set_if_has_field(doc, "Customer", "territory", territory, changed)

	table_field, child_doctype = _party_account_table("Customer")
	if table_field and child_doctype and account:
		rows = [row for row in doc.get(table_field) if row.get("company") == company]
		row = rows[0] if rows else doc.append(table_field, {})
		if not rows:
			changed.append(table_field)
		_set_child_if_has_field(row, child_doctype, "company", company, changed)
		_set_child_if_has_field(row, child_doctype, "account", account, changed)
	else:
		_warn("Customer default account child table is not available or receivable account is missing.")

	doc.flags.ignore_guardian_identity_validation = True
	if is_new:
		_insert_doc(doc, f"Customer {CUSTOMER_NAME}")
	elif changed:
		_save_doc(doc, f"Customer {doc.name}: {', '.join(sorted(set(changed)))}")
	else:
		_skipped(f"Customer {doc.name}")
	return doc.name


def ensure_supplier_group(name: str, parent: str | None = None):
	"""Create a Supplier Group if missing."""
	_meta("Supplier Group")
	if frappe.db.exists("Supplier Group", name):
		_skipped(f"Supplier Group {name}")
		return name

	doc = frappe.new_doc("Supplier Group")
	_set_if_has_field(doc, "Supplier Group", "supplier_group_name", name)
	_set_if_has_field(doc, "Supplier Group", "is_group", 0)
	parent = parent or _first_root_node("Supplier Group", "parent_supplier_group")
	if parent:
		_set_if_has_field(doc, "Supplier Group", "parent_supplier_group", parent)
	_insert_doc(doc, f"Supplier Group {name}")
	return doc.name


def _item_group_defaults_table() -> tuple[str | None, str | None]:
	return _table_field("Item Group", ("company",), ("Item Default",))


def _item_group_root() -> str | None:
	if frappe.db.exists("Item Group", "All Item Groups"):
		return "All Item Groups"
	return _first_root_node("Item Group", "parent_item_group")


def ensure_item_group(
	company: str,
	item_group: str,
	parent_item_group: str | None,
	income_number: str | None = None,
	expense_number: str | None = None,
	inventory_number: str | None = None,
	warehouse_name: str | None = None,
):
	"""Create/update an Item Group and its Item Default row when supported."""
	_meta("Item Group")
	_meta("Item Default")
	parent_item_group = parent_item_group or _item_group_root()

	if frappe.db.exists("Item Group", item_group):
		doc = frappe.get_doc("Item Group", item_group)
		is_new = False
	else:
		doc = frappe.new_doc("Item Group")
		_set_if_has_field(doc, "Item Group", "item_group_name", item_group)
		is_new = True

	changed: list[str] = []
	_set_if_has_field(doc, "Item Group", "is_group", 0, changed)
	if parent_item_group:
		_set_if_has_field(doc, "Item Group", "parent_item_group", parent_item_group, changed)

	table_field, child_doctype = _item_group_defaults_table()
	if table_field and child_doctype:
		rows = [row for row in doc.get(table_field) if row.get("company") == company]
		row = rows[0] if rows else doc.append(table_field, {})
		if not rows:
			changed.append(table_field)
		_set_child_if_has_field(row, child_doctype, "company", company, changed)

		income_account = get_account_name(company, income_number) if income_number else None
		expense_account = get_account_name(company, expense_number) if expense_number else None
		inventory_account = get_account_name(company, inventory_number) if inventory_number else None
		warehouse = _warehouse_by_label(company, warehouse_name) if warehouse_name else None

		if income_account:
			_set_child_if_has_field(row, child_doctype, "income_account", income_account, changed)
		if expense_account:
			_set_child_if_has_field(row, child_doctype, "expense_account", expense_account, changed)
			_set_child_if_has_field(row, child_doctype, "default_cogs_account", expense_account, changed)
			_set_child_if_has_field(row, child_doctype, "purchase_expense_account", expense_account, changed)
		if inventory_account:
			_set_child_if_has_field(row, child_doctype, "default_inventory_account", inventory_account, changed)
		if warehouse:
			_set_child_if_has_field(row, child_doctype, "default_warehouse", warehouse, changed)
	else:
		_warn("Item Group default child table was not found.")

	if is_new:
		_insert_doc(doc, f"Item Group {item_group}")
	elif changed:
		_save_doc(doc, f"Item Group {item_group}: {', '.join(sorted(set(changed)))}")
	else:
		_skipped(f"Item Group {item_group}")
	return doc.name


def ensure_pos_profile(company: str) -> str | None:
	"""Create/update the main POS Profile and payment rows."""
	_meta("POS Profile")
	customer = ensure_customer(company)
	warehouse = _warehouse_by_label(company, "المخزن الرئيسي")
	write_off_account = get_account_name(company, "654000")
	cost_center = _cost_center_by_label(company, "المبيعات / المحل")
	currency = frappe.db.get_value("Company", company, "default_currency")

	if frappe.db.exists("POS Profile", POS_PROFILE_NAME):
		doc = frappe.get_doc("POS Profile", POS_PROFILE_NAME)
		is_new = False
	else:
		doc = frappe.new_doc("POS Profile")
		doc.name = POS_PROFILE_NAME
		is_new = True

	changed: list[str] = []
	_set_if_has_field(doc, "POS Profile", "company", company, changed)
	_set_if_has_field(doc, "POS Profile", "customer", customer, changed)
	_set_if_has_field(doc, "POS Profile", "warehouse", warehouse, changed)
	_set_if_has_field(doc, "POS Profile", "write_off_account", write_off_account, changed)
	_set_if_has_field(doc, "POS Profile", "write_off_cost_center", cost_center, changed)
	_set_if_has_field(doc, "POS Profile", "cost_center", cost_center, changed)
	if currency:
		_set_if_has_field(doc, "POS Profile", "currency", currency, changed)

	table_field, child_doctype = _table_field("POS Profile", ("mode_of_payment",), ("POS Payment Method",))
	if not table_field or not child_doctype:
		_warn("POS Profile payment child table was not found.")
		return doc.name if not is_new else None

	required_payments = ("نقداً", "بطاقة بنكية", "تحويل بنكي", "محفظة إلكترونية / Mobile Money")
	for row in doc.get(table_field):
		if _has_field(child_doctype, "default") and row.get("mode_of_payment") != "نقداً" and row.get("default"):
			row.set("default", 0)
			changed.append(table_field)

	for mode in required_payments:
		rows = [row for row in doc.get(table_field) if row.get("mode_of_payment") == mode]
		row = rows[0] if rows else doc.append(table_field, {})
		if not rows:
			changed.append(table_field)
		if len(rows) > 1:
			_warn(f"POS Profile {POS_PROFILE_NAME} has duplicate rows for {mode}; first row updated only.")
		_set_child_if_has_field(row, child_doctype, "mode_of_payment", mode, changed)
		_set_child_if_has_field(row, child_doctype, "default", 1 if mode == "نقداً" else 0, changed)
		_set_child_if_has_field(row, child_doctype, "allow_in_returns", 1, changed)

	if is_new:
		_insert_doc(doc, f"POS Profile {POS_PROFILE_NAME}")
	elif changed:
		_save_doc(doc, f"POS Profile {POS_PROFILE_NAME}: {', '.join(sorted(set(changed)))}")
	else:
		_skipped(f"POS Profile {POS_PROFILE_NAME}")
	return doc.name


def _tax_template_name(company: str, title: str) -> str:
	return f"{title} - {get_company_abbr(company)}"


def _ensure_one_tax_template(company: str, doctype: str, title: str, account_number: str):
	_meta(doctype)
	account = get_account_name(company, account_number)
	if not account:
		_error(f"Cannot create tax template {title}; account_number {account_number} was not found.")
		return

	template_name = _tax_template_name(company, title)
	if frappe.db.exists(doctype, template_name):
		doc = frappe.get_doc(doctype, template_name)
		is_new = False
	else:
		doc = frappe.new_doc(doctype)
		is_new = True

	changed: list[str] = []
	_set_if_has_field(doc, doctype, "title", title, changed)
	_set_if_has_field(doc, doctype, "company", company, changed)
	_set_if_has_field(doc, doctype, "disabled", 0, changed)

	table_field, child_doctype = _table_field(doctype, ("account_head", "rate"), ())
	if not table_field or not child_doctype:
		_warn(f"{doctype} taxes child table was not found.")
		return

	rows = [row for row in doc.get(table_field) if row.get("account_head") == account]
	row = rows[0] if rows else doc.append(table_field, {})
	if not rows:
		changed.append(table_field)
	_set_child_if_has_field(row, child_doctype, "charge_type", "On Net Total", changed)
	_set_child_if_has_field(row, child_doctype, "account_head", account, changed)
	_set_child_if_has_field(row, child_doctype, "rate", 0, changed)
	_set_child_if_has_field(row, child_doctype, "description", f"{title} - يضبط المحاسب النسبة حسب القانون المحلي", changed)
	_set_child_if_has_field(row, child_doctype, "category", "Total", changed)
	_set_child_if_has_field(row, child_doctype, "add_deduct_tax", "Add", changed)

	if is_new:
		_insert_doc(doc, f"{doctype} {title}")
	elif changed:
		_save_doc(doc, f"{doctype} {title}: {', '.join(sorted(set(changed)))}")
	else:
		_skipped(f"{doctype} {title}")


def ensure_tax_templates(company: str):
	"""Create VAT templates with zero rates for accountant configuration.

	Accountants must configure VAT rates according to the applicable local law.
	"""
	for doctype, title, account_number in VAT_TEMPLATE_ROWS:
		_ensure_one_tax_template(company, doctype, title, account_number)


def ensure_cashier_accounting_dimension():
	"""Create a non-mandatory Cashier Accounting Dimension when possible."""
	_meta("Accounting Dimension")
	if not _doctype_exists("Accounting Dimension"):
		_warn("Accounting Dimension DocType is not available.")
		return None

	for filters in (
		{"label": "Cashier"},
		{"label": "الكاشير"},
		{"fieldname": "cashier"},
	):
		existing = frappe.db.get_value("Accounting Dimension", filters, "name")
		if existing:
			_skipped(f"Accounting Dimension {existing}")
			return existing

	reference_doctypes = ["User"]
	if _doctype_exists("Employee"):
		reference_doctypes.append("Employee")

	last_error: Exception | None = None
	for reference_doctype in reference_doctypes:
		if not _doctype_exists(reference_doctype):
			continue
		try:
			doc = frappe.new_doc("Accounting Dimension")
			_set_if_has_field(doc, "Accounting Dimension", "label", "Cashier")
			_set_if_has_field(doc, "Accounting Dimension", "document_type", reference_doctype)
			_set_if_has_field(doc, "Accounting Dimension", "disabled", 0)
			_insert_doc(doc, f"Accounting Dimension Cashier ({reference_doctype})")
			return doc.name
		except Exception as exc:
			last_error = exc
			_warn(f"Could not create Cashier Accounting Dimension using {reference_doctype}: {exc}")

	if last_error:
		_warn(f"Cashier Accounting Dimension was not created: {last_error}")
	return None


def _ensure_cost_centers(company: str):
	root = ensure_cost_center(company, company, parent=None, is_group=1)
	for name in ("المبيعات / المحل", "الصيدلية البيطرية", "العيادة البيطرية", "الأونلاين والتوصيل", "الإدارة"):
		ensure_cost_center(company, name, parent=root or company, is_group=0)
	for fieldname, cost_center_label in COMPANY_COST_CENTER_DEFAULTS.items():
		cost_center = _cost_center_by_label(company, cost_center_label)
		if cost_center:
			_ensure_company_value(company, fieldname, cost_center)
		else:
			_warn(f"Cannot set Company.{fieldname}; Cost Center {cost_center_label} was not found.")


def _ensure_warehouses(company: str):
	root_label = f"مخازن {company}"
	root = ensure_warehouse(company, root_label, parent=None, is_group=1)
	for name in ("المخزن الرئيسي", "رفوف الصيدلية", "ثلاجة اللقاحات", "مخزن المرتجعات", "مخزن التالف والمنتهي"):
		ensure_warehouse(company, name, parent=root or root_label, is_group=0)
	ensure_warehouse(company, "مخزون في الطريق", parent=root or root_label, is_group=0, warehouse_type="Transit")


def _ensure_company_defaults(company: str):
	for fieldname, account_number in COMPANY_ACCOUNT_DEFAULTS.items():
		ensure_company_default(company, fieldname, account_number)
	_ensure_company_value(company, "enable_perpetual_inventory", 1)


def _ensure_accounts(company: str):
	for message in DUPLICATE_GROUP_NUMBER_WARNINGS:
		_warn(message)
	for row in ACCOUNT_ROWS:
		ensure_account(
			company=company,
			number=row["number"],
			arabic_name=row["name"],
			root_type=row["root_type"],
			parent_number=row.get("parent"),
			is_group=row.get("is_group", 0),
			account_type=row.get("account_type"),
		)


def _ensure_modes_of_payment(company: str):
	for name, mop_type, account_number in MOP_ROWS:
		ensure_mode_of_payment(name, mop_type, company, account_number)


def _ensure_supplier_groups():
	for supplier_group in SUPPLIER_GROUPS:
		ensure_supplier_group(supplier_group)


def _ensure_item_groups(company: str):
	parent = _item_group_root()
	for item_group, income_number, expense_number, inventory_number, warehouse_name in ITEM_GROUP_ROWS:
		ensure_item_group(
			company,
			item_group,
			parent,
			income_number=income_number,
			expense_number=expense_number,
			inventory_number=inventory_number,
			warehouse_name=warehouse_name,
		)


@frappe.whitelist()
def setup_french_pcg_vet_store(
	company: str = DEFAULT_COMPANY,
	create_pos_profile: bool = True,
	create_tax_templates: bool = True,
	create_cashier_dimension: bool = True,
) -> dict[str, Any]:
	"""Build/update the Alkokh Vet Store French-PCG setup idempotently."""
	global _ACTIVE_SUMMARY
	company = company or DEFAULT_COMPANY
	_ACTIVE_SUMMARY = _new_summary(company)
	LOGGER.info("Starting French PCG setup for company %s", company)

	try:
		_prime_required_metadata()
		if not frappe.db.exists("Company", company):
			frappe.throw(f"Company '{company}' does not exist. Create the Company before running this setup.")

		get_company_abbr(company)
		_ensure_accounts(company)
		_ensure_cost_centers(company)
		_ensure_warehouses(company)
		_ensure_company_defaults(company)
		_ensure_modes_of_payment(company)
		ensure_customer(company)
		_ensure_supplier_groups()
		_ensure_item_groups(company)

		if create_pos_profile:
			ensure_pos_profile(company)
		else:
			_skipped("POS Profile setup disabled by argument")

		if create_tax_templates:
			ensure_tax_templates(company)
		else:
			_skipped("Tax template setup disabled by argument")

		if create_cashier_dimension:
			ensure_cashier_accounting_dimension()
		else:
			_skipped("Cashier Accounting Dimension setup disabled by argument")

		validation = validate_setup(company)
		for warning in validation.get("warnings", []):
			_warn(f"Validation: {warning}")
		for error in validation.get("errors", []):
			_error(f"Validation: {error}")

		LOGGER.info("Finished French PCG setup for company %s", company)
		return _ACTIVE_SUMMARY
	except Exception as exc:
		_error("French PCG setup failed", exc)
		raise
	finally:
		_ACTIVE_SUMMARY = None


def validate_setup(company: str) -> dict[str, Any]:
	"""Validate the resulting setup without mutating documents."""
	_prime_required_metadata()
	result: dict[str, Any] = {"company": company, "warnings": [], "errors": [], "checks": []}

	def fail(message: str):
		result["errors"].append(message)

	def warn(message: str):
		result["warnings"].append(message)

	company_doc = frappe.get_doc("Company", company)
	for fieldname, account_number in COMPANY_ACCOUNT_DEFAULTS.items():
		if not _has_field("Company", fieldname):
			continue
		expected = get_account_name(company, account_number)
		value = company_doc.get(fieldname)
		if not expected:
			fail(f"Account {account_number} for Company.{fieldname} is missing.")
			continue
		if value != expected:
			fail(f"Company.{fieldname} is {value!r}; expected {expected!r}.")
		if expected and frappe.db.get_value("Account", expected, "is_group"):
			fail(f"Company.{fieldname} points to group Account {expected}.")

	for account_number, expected_type in ACCOUNT_TYPE_VALIDATION.items():
		account = get_account_name(company, account_number)
		if not account:
			fail(f"Account {account_number} is missing.")
			continue
		actual_type = frappe.db.get_value("Account", account, "account_type")
		if actual_type != expected_type:
			fail(f"Account {account_number} ({account}) has account_type {actual_type!r}; expected {expected_type!r}.")

	if frappe.db.exists("POS Profile", POS_PROFILE_NAME):
		pos = frappe.get_doc("POS Profile", POS_PROFILE_NAME)
		table_field, _child_doctype = _table_field("POS Profile", ("mode_of_payment",), ("POS Payment Method",))
		if table_field and not pos.get(table_field):
			fail(f"POS Profile {POS_PROFILE_NAME} has no payment rows.")
	else:
		warn(f"POS Profile {POS_PROFILE_NAME} is missing.")

	table_field, _child_doctype = _table_field("Mode of Payment", ("company", "default_account"), ("Mode of Payment Account",))
	for name, _mop_type, account_number in MOP_ROWS:
		if not frappe.db.exists("Mode of Payment", name):
			fail(f"Mode of Payment {name} is missing.")
			continue
		if not table_field:
			warn("Mode of Payment default account table is not supported.")
			continue
		expected = get_account_name(company, account_number)
		doc = frappe.get_doc("Mode of Payment", name)
		if not any(row.get("company") == company and row.get("default_account") == expected for row in doc.get(table_field)):
			fail(f"Mode of Payment {name} has no default account row for {company}.")

	item_table, _item_child = _item_group_defaults_table()
	for item_group, *_rest in ITEM_GROUP_ROWS:
		if not frappe.db.exists("Item Group", item_group):
			fail(f"Item Group {item_group} is missing.")
			continue
		if item_table:
			doc = frappe.get_doc("Item Group", item_group)
			if not any(row.get("company") == company for row in doc.get(item_table)):
				fail(f"Item Group {item_group} has no Item Default row for {company}.")
		else:
			warn("Item Group defaults table is not supported.")

	result["checks"].append("company_default_accounts")
	result["checks"].append("critical_account_types")
	result["checks"].append("pos_profile_payments")
	result["checks"].append("mode_of_payment_defaults")
	result["checks"].append("item_group_defaults")
	return result


@frappe.whitelist()
def smoke_check_french_pcg_vet_store(company: str = DEFAULT_COMPANY) -> dict[str, Any]:
	"""Return a compact, read-only smoke check for the French PCG setup."""
	company = company or DEFAULT_COMPANY
	if not frappe.db.exists("Company", company):
		frappe.throw(f"Company '{company}' does not exist.")
	_prime_required_metadata()

	expected_numbers = tuple(row["number"] for row in ACCOUNT_ROWS)
	missing_numbers = [number for number in expected_numbers if not get_account_name(company, number)]

	company_doc = frappe.get_doc("Company", company)
	company_defaults = {}
	for fieldname, account_number in COMPANY_ACCOUNT_DEFAULTS.items():
		if not _has_field("Company", fieldname):
			company_defaults[fieldname] = {"supported": False, "ok": None}
			continue
		expected = get_account_name(company, account_number)
		value = company_doc.get(fieldname)
		company_defaults[fieldname] = {
			"supported": True,
			"expected_account_number": account_number,
			"expected_account": expected,
			"value": value,
			"ok": bool(expected and value == expected),
		}

	pos_status: dict[str, Any] = {"exists": bool(frappe.db.exists("POS Profile", POS_PROFILE_NAME))}
	if pos_status["exists"]:
		pos = frappe.get_doc("POS Profile", POS_PROFILE_NAME)
		table_field, _child = _table_field("POS Profile", ("mode_of_payment",), ("POS Payment Method",))
		pos_status["payment_count"] = len(pos.get(table_field) or []) if table_field else None
		pos_status["payments_supported"] = bool(table_field)

	mop_status = {}
	table_field, _child = _table_field("Mode of Payment", ("company", "default_account"), ("Mode of Payment Account",))
	for name, _mop_type, account_number in MOP_ROWS:
		exists = bool(frappe.db.exists("Mode of Payment", name))
		expected = get_account_name(company, account_number)
		ok = False
		if exists and table_field:
			doc = frappe.get_doc("Mode of Payment", name)
			ok = any(row.get("company") == company and row.get("default_account") == expected for row in doc.get(table_field))
		mop_status[name] = {"exists": exists, "expected_account_number": account_number, "default_ok": ok}

	item_group_status = {}
	item_table, _item_child = _item_group_defaults_table()
	for item_group, income_number, expense_number, inventory_number, warehouse_name in ITEM_GROUP_ROWS:
		exists = bool(frappe.db.exists("Item Group", item_group))
		has_default = False
		if exists and item_table:
			doc = frappe.get_doc("Item Group", item_group)
			has_default = any(row.get("company") == company for row in doc.get(item_table))
		item_group_status[item_group] = {
			"exists": exists,
			"defaults_supported": bool(item_table),
			"has_company_default": has_default,
			"income_account_number": income_number,
			"expense_account_number": expense_number,
			"inventory_account_number": inventory_number,
			"warehouse": warehouse_name,
		}

	return {
		"company": company,
		"accounts": {
			"expected_count": len(expected_numbers),
			"found_count": len(expected_numbers) - len(missing_numbers),
			"missing_account_numbers": missing_numbers,
			"duplicate_expected_numbers": [number for number, count in Counter(expected_numbers).items() if count > 1],
		},
		"company_default_fields": company_defaults,
		"pos_profile": pos_status,
		"mode_of_payment": mop_status,
		"item_group_defaults": item_group_status,
	}
