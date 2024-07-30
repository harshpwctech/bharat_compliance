import frappe
from frappe import _
from frappe.utils import cint
from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import PurchaseInvoice
from erpnext.accounts.doctype.tax_withholding_category.tax_withholding_category import (
    get_party_details,
    get_tax_withholding_details,
    get_cost_center,
    get_tax_row_for_tds,
    get_advance_vouchers,
    get_taxes_deducted_on_advances_allocated,
    get_deducted_tax,
    get_lower_deduction_certificate,
    is_valid_certificate,
    get_lower_deduction_amount,
    normal_round
    )

class CustomPurchaseInvoice(PurchaseInvoice):
    def validate(self):
        super().validate()
        if self.item_wise_tds:
            self.custom_set_tax_withholding()
        else:
             self.set("tax_withholding_details", [])
    
    def custom_set_tax_withholding(self):
        if not self.apply_tds:
            return
        if self.apply_tds and not self.get("tax_withholding_category"):
            self.tax_withholding_category = frappe.db.get_value(
                "Supplier", self.supplier, "tax_withholding_category"
            )

        if not (self.tax_withholding_category and self.item_wise_tds):
            return

        if self.item_wise_tds:
            self.tax_withholding_category = None
            item_wise_tax_withheld, tax_withholding_details, advance_taxes, voucher_wise_amount = get_item_tax_withholding_details(
                self
            )
            self.tax_withholding_details = []
            if len(item_wise_tax_withheld):
                for i in item_wise_tax_withheld:
                    self.append("tax_withholding_details", i)


            if not tax_withholding_details:
                return
            self.set("taxes", [])
            for t in tax_withholding_details:
                super().allocate_advance_tds(t, advance_taxes)
                self.append("taxes", t)


        ## Add pending vouchers on which tax was withheld
        self.set("tax_withheld_vouchers", [])

        for voucher_no, voucher_details in voucher_wise_amount.items():
            self.append(
                "tax_withheld_vouchers",
                {
                    "voucher_name": voucher_no,
                    "voucher_type": voucher_details.get("voucher_type"),
                    "taxable_amount": voucher_details.get("amount"),
                },
            )

        # calculate totals again after applying TDS
        super().calculate_taxes_and_totals()

def get_item_tax_withholding_details(inv):
    tax_withholding_categories = {}
    
    for i in inv.items:
        if i.tax_withholding_category:
            tax_withholding_categories.setdefault(i.tax_withholding_category, 0)
            tax_withholding_categories[i.tax_withholding_category] += i.base_net_amount
        else:
            frappe.msgprint(
            _(
                "Skipping Item {0} as there is no Tax Withholding Category set in it."
            ).format(i.item_code)
        )
    if not tax_withholding_categories:
        return
    pan_no = ""
    parties = []
    party_type, party = get_party_details(inv)
    has_pan_field = frappe.get_meta(party_type).has_field("pan")
    tax_withheld_categories = []
    tax_rows = []
    tax_deducted_on_advances = []
    voucher_wise_amount = {}
    if has_pan_field:
        pan_no = frappe.db.get_value(party_type, party, "pan")
    if pan_no:
        parties = frappe.get_all(party_type, filters={"pan": pan_no}, pluck="name")
    if not parties:
        parties.append(party)
    posting_date = inv.get("posting_date") or inv.get("transaction_date")
    for details, amount in tax_withholding_categories.items():
        tax_details = get_tax_withholding_details(details, posting_date, inv.company)
        if not tax_details:
            frappe.msgprint(
                _(
                    "Skipping Tax Withholding Category {0} as there is no associated account set for Company {1} in it."
                ).format(details, inv.company)
            )
            continue
        tax_amount, tax_deducted, tax_deducted_on_advances, voucher_wise_amount = get_tax_amount(
            party_type, parties, inv, tax_details, posting_date, pan_no, amount
        )
        tax_withholding_category = {
            "tax_withholding_category": details,
            "net_amount": amount,
            "tax_withheld": tax_amount
        }
        tax_withheld_categories.append(tax_withholding_category)
        tax_row = get_tax_row_for_tds(tax_details, tax_amount)
        cost_center = get_cost_center(inv)
        tax_row.update({"cost_center": cost_center})
        tax_rows.append(tax_row)
        
    return tax_withheld_categories, tax_rows, tax_deducted_on_advances, voucher_wise_amount

def get_tax_amount(party_type, parties, inv, tax_details, posting_date, pan_no=None, net_amount=0):
    vouchers, voucher_wise_amount = get_invoice_vouchers(
        parties, tax_details, inv, party_type=party_type
    )
    advance_vouchers = get_advance_vouchers(
        parties,
        company=inv.company,
        from_date=tax_details.from_date,
        to_date=tax_details.to_date,
        party_type=party_type,
    )
    taxable_vouchers = vouchers + advance_vouchers
    tax_deducted_on_advances = 0

    if inv.doctype == "Purchase Invoice":
        tax_deducted_on_advances = get_taxes_deducted_on_advances_allocated(inv, tax_details)

    tax_deducted = 0
    if taxable_vouchers:
        tax_deducted = get_deducted_tax(taxable_vouchers, tax_details)

    tax_amount = 0

    if party_type == "Supplier":
        ldc = get_lower_deduction_certificate(inv.company, tax_details, pan_no)
        if tax_deducted:
            if inv.item_wise_tds:
                net_total = net_amount
            else:
                net_total = inv.tax_withholding_net_total
            if ldc:
                limit_consumed = get_limit_consumed(ldc, parties)
                if is_valid_certificate(ldc, posting_date, limit_consumed):
                    tax_amount = get_lower_deduction_amount(
                        net_total, limit_consumed, ldc.certificate_limit, ldc.rate, tax_details
                    )
                else:
                    tax_amount = net_total * tax_details.rate / 100
            else:
                tax_amount = net_total * tax_details.rate / 100

            # once tds is deducted, not need to add vouchers in the invoice
            voucher_wise_amount = {}
        else:
            tax_amount = get_tds_amount(ldc, parties, inv, tax_details, vouchers)

    elif party_type == "Customer":
        if tax_deducted:
            # if already TCS is charged, then amount will be calculated based on 'Previous Row Total'
            tax_amount = 0
        else:
            #  if no TCS has been charged in FY,
            # then chargeable value is "prev invoices + advances" value which cross the threshold
            tax_amount = get_tcs_amount(parties, inv, tax_details, vouchers, advance_vouchers)

    if cint(tax_details.round_off_tax_amount):
        tax_amount = normal_round(tax_amount)

    return tax_amount, tax_deducted, tax_deducted_on_advances, voucher_wise_amount


def get_invoice_vouchers(parties, tax_details, inv, party_type="Supplier"):
    doctype = "Purchase Invoice" if party_type == "Supplier" else "Sales Invoice"
    field = (
        "base_tax_withholding_net_total as base_net_total"
        if party_type == "Supplier"
        else "base_net_total"
    )
    voucher_wise_amount = {}
    vouchers = []

    # filters = {
    #     "company": inv.company,
    #     frappe.scrub(party_type): ["in", parties],
    #     "posting_date": ["between", (tax_details.from_date, tax_details.to_date)],
    #     "is_opening": "No",
    #     "docstatus": 1,
    # }
    filters = [
        [doctype, "company", "=", inv.company],
        [doctype, frappe.scrub(party_type), "in", parties],
        [doctype, "posting_date", "between", (tax_details.from_date, tax_details.to_date)],
        [doctype, "is_opening", "=", "No"],
        [doctype, "docstatus", "=", 1],
    ]

    if doctype != "Sales Invoice":
        filters.extend(
            [[doctype, "apply_tds", "=", 1],
            [doctype, "tax_withholding_category", "=", tax_details.get("tax_withholding_category")]]
        )
    invoices_details = frappe.get_all(doctype, filters=filters, fields=["name", field])
    additional_filters = filters.copy()
    if doctype != "Sales Invoice":
        additional_invoices_details = frappe.get_all("Tax Withholding Detail", filters={"tax_withholding_category": tax_details.get("tax_withholding_category"), "docstatus": 1}, fields=["net_amount as base_net_total", "parent as name"])
        invoices_details += additional_invoices_details
    
    for d in invoices_details:
        vouchers.append(d.name)
        voucher_wise_amount.update({d.name: {"amount": d.base_net_total, "voucher_type": doctype}})

    journal_entries_details = frappe.db.sql(
        """
        SELECT j.name, ja.credit - ja.debit AS amount
            FROM `tabJournal Entry` j, `tabJournal Entry Account` ja
        WHERE
            j.name = ja.parent
            AND j.docstatus = 1
            AND j.is_opening = 'No'
            AND j.posting_date between %s and %s
            AND ja.party in %s
            AND j.apply_tds = 1
            AND j.tax_withholding_category = %s
    """,
        (
            tax_details.from_date,
            tax_details.to_date,
            tuple(parties),
            tax_details.get("tax_withholding_category"),
        ),
        as_dict=1,
    )

    if journal_entries_details:
        for d in journal_entries_details:
            vouchers.append(d.name)
            voucher_wise_amount.update({d.name: {"amount": d.amount, "voucher_type": "Journal Entry"}})

    return vouchers, voucher_wise_amount

def get_limit_consumed(ldc, parties):
	limit_consumed = frappe.db.get_value(
		"Purchase Invoice",
		{
			"supplier": ("in", parties),
			"apply_tds": 1,
			"docstatus": 1,
			"tax_withholding_category": ldc.tax_withholding_category,
			"posting_date": ("between", (ldc.valid_from, ldc.valid_upto)),
			"company": ldc.company,
		},
		"sum(tax_withholding_net_total)",
	)

	return limit_consumed

def get_tds_amount(ldc, parties, inv, tax_details, vouchers):
	tds_amount = 0
	invoice_filters = {"name": ("in", vouchers), "docstatus": 1, "apply_tds": 1}

	## for TDS to be deducted on advances
	payment_entry_filters = {
		"party_type": "Supplier",
		"party": ("in", parties),
		"docstatus": 1,
		"apply_tax_withholding_amount": 1,
		"unallocated_amount": (">", 0),
		"posting_date": ["between", (tax_details.from_date, tax_details.to_date)],
		"tax_withholding_category": tax_details.get("tax_withholding_category"),
	}

	field = "sum(tax_withholding_net_total)"

	if cint(tax_details.consider_party_ledger_amount):
		invoice_filters.pop("apply_tds", None)
		field = "sum(grand_total)"

		payment_entry_filters.pop("apply_tax_withholding_amount", None)
		payment_entry_filters.pop("tax_withholding_category", None)

	supp_credit_amt = frappe.db.get_value("Purchase Invoice", invoice_filters, field) or 0.0

	supp_jv_credit_amt = (
		frappe.db.get_value(
			"Journal Entry Account",
			{
				"parent": ("in", vouchers),
				"docstatus": 1,
				"party": ("in", parties),
				"reference_type": ("!=", "Purchase Invoice"),
			},
			"sum(credit_in_account_currency - debit_in_account_currency)",
		)
		or 0.0
	)

	# Get Amount via payment entry
	payment_entry_amounts = frappe.db.get_all(
		"Payment Entry",
		filters=payment_entry_filters,
		fields=["sum(unallocated_amount) as amount", "payment_type"],
		group_by="payment_type",
	)

	supp_credit_amt += supp_jv_credit_amt
	supp_credit_amt += inv.tax_withholding_net_total

	for type in payment_entry_amounts:
		if type.payment_type == "Pay":
			supp_credit_amt += type.amount
		else:
			supp_credit_amt -= type.amount

	threshold = tax_details.get("threshold", 0)
	cumulative_threshold = tax_details.get("cumulative_threshold", 0)

	if inv.doctype != "Payment Entry":
		tax_withholding_net_total = inv.base_tax_withholding_net_total
	else:
		tax_withholding_net_total = inv.tax_withholding_net_total

	if (threshold and tax_withholding_net_total >= threshold) or (
		cumulative_threshold and supp_credit_amt >= cumulative_threshold
	):
		if (cumulative_threshold and supp_credit_amt >= cumulative_threshold) and cint(
			tax_details.tax_on_excess_amount
		):
			# Get net total again as TDS is calculated on net total
			# Grand is used to just check for threshold breach
			net_total = (
				frappe.db.get_value("Purchase Invoice", invoice_filters, "sum(tax_withholding_net_total)")
				or 0.0
			)
			net_total += inv.tax_withholding_net_total
			supp_credit_amt = net_total - cumulative_threshold

		if ldc and is_valid_certificate(ldc, inv.get("posting_date") or inv.get("transaction_date"), 0):
			tds_amount = get_lower_deduction_amount(
				supp_credit_amt, 0, ldc.certificate_limit, ldc.rate, tax_details
			)
		else:
			tds_amount = supp_credit_amt * tax_details.rate / 100 if supp_credit_amt > 0 else 0

	return tds_amount

