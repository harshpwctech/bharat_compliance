# Copyright (c) 2024, pwctech technologies private limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


class TaxWithholdingSetting(Document):
	def validate(self):
		if self.item_wise_tds:
			#creating custom fields in purchase invoice
			create_tds_custom_fields()
	
def create_tds_custom_fields():
	custom_fields = {
		"Purchase Invoice": [
			dict(
				fieldname="item_wise_tds",
				label="Apply Item wise Tax Withholding Amount",
				fieldtype="Check",
				insert_after="apply_tds",
				print_hide=1,
			),
			dict(
				fieldname="tax_withholding_details",
				label="Tax Withholding Details",
				fieldtype="Table",
				insert_after="taxes",
				options="Tax Withholding Detail",
				read_only=1,
				print_hide=1,
			),
		],
		"Purchase Invoice Item": [
			dict(
				fieldname="tax_withholding_category",
				label="Tax Withholding Category",
				fieldtype="Link",
				insert_after="apply_tds",
				print_hide=1,
				read_only=1,
				options="Tax Withholding Category"
			),
		],
		"Item Supplier": [
			dict(
				fieldname="tax_withholding_category",
				label="Tax Withholding Category",
				fieldtype="Link",
				insert_after="supplier_part_no",
				print_hide=1,
				options="Tax Withholding Category"
			),
		]
	}

	create_custom_fields(custom_fields)
