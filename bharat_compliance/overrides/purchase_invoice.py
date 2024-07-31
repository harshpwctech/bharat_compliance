import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt
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
        self.set("tax_withholding_details", [])
        if self.item_wise_tds:
            self.custom_set_tax_withholding()
        else:
             return
    
    def custom_set_tax_withholding(self):
        if not self.apply_tds:
            return
        if self.apply_tds and not self.get("tax_withholding_category"):
            self.tax_withholding_category = frappe.db.get_value(
                "Supplier", self.supplier, "tax_withholding_category"
            )

        tax_withholding_categories = {}
        for i in self.items:
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
            return super().calculate_taxes_and_totals()
        self.tax_withholding_category = None
        accounts = set()
        tax_withholding_details = {}
        for tax_withholding_category, net_amount in tax_withholding_categories.items():
            tax_withholding_detail, advance_taxes, voucher_wise_amount = get_item_tax_withholding_details(
                self, tax_withholding_category, net_amount
            )
            if not tax_withholding_detail:
                continue
            tax_withholding_data = {
                "tax_withholding_category": tax_withholding_category,
                "net_amount": net_amount,
                "tax_withheld": tax_withholding_detail.get("tax_amount")
            }
            self.append("tax_withholding_details", tax_withholding_data)
            self.allocate_advance_tds(tax_withholding_detail, advance_taxes)
            tax_withholding_details.setdefault(tax_withholding_detail.get("account_head"), {"amount":0, "tax_rows": []})
            tax_withholding_details[tax_withholding_detail.get("account_head")]["amount"] += tax_withholding_detail.get("tax_amount")
            tax_withholding_details[tax_withholding_detail.get("account_head")]["tax_rows"].append(tax_withholding_detail)
            
        for d in self.taxes:
            for account, details in tax_withholding_details.items():
                if d.account_head == account:
                    d.update(details["tax_rows"][0])
                    d.tax_amount = details["amount"]
            accounts.add(d.account_head)
        for account, details in tax_withholding_details.items():
            if account not in list(accounts):
                tax_row = details["tax_rows"][0]
                tax_row["tax_amount"] = details["amount"]
                self.append("taxes", tax_row)
        ## Add pending vouchers on which tax was withheld
        for voucher_no, voucher_details in voucher_wise_amount.items():
            self.append(
                "tax_withheld_vouchers",
                {
                    "voucher_name": voucher_no,
                    "voucher_type": voucher_details.get("voucher_type"),
                    "taxable_amount": voucher_details.get("amount"),
                },
            )

        to_remove = [
            d
            for d in self.taxes
            if not d.tax_amount and d.account_head not in list(accounts)
        ]

        for d in to_remove:
            self.remove(d)
        # calculate totals again after applying TDS
        super().calculate_taxes_and_totals()
            
    
    def allocate_advance_tds(self, tax_withholding_details, advance_taxes):
        for tax in advance_taxes:
            allocated_amount = 0
            pending_amount = flt(tax.tax_amount - tax.allocated_amount)
            if flt(tax_withholding_details.get("tax_amount")) >= pending_amount:
                tax_withholding_details["tax_amount"] -= pending_amount
                allocated_amount = pending_amount
            elif (
                flt(tax_withholding_details.get("tax_amount"))
                and flt(tax_withholding_details.get("tax_amount")) < pending_amount
            ):
                allocated_amount = tax_withholding_details["tax_amount"]
                tax_withholding_details["tax_amount"] = 0

            self.append(
                "advance_tax",
                {
                    "reference_type": "Payment Entry",
                    "reference_name": tax.parent,
                    "reference_detail": tax.name,
                    "account_head": tax.account_head,
                    "allocated_amount": allocated_amount,
                },
            )


def get_item_tax_withholding_details(inv, tax_withholding_category, net_amount):
    pan_no = ""
    parties = []
    party_type, party = get_party_details(inv)
    has_pan_field = frappe.get_meta(party_type).has_field("pan")
    voucher_wise_amount = {}
    if has_pan_field:
        pan_no = frappe.db.get_value(party_type, party, "pan")
    if pan_no:
        parties = frappe.get_all(party_type, filters={"pan": pan_no}, pluck="name")
    if not parties:
        parties.append(party)
    posting_date = inv.get("posting_date") or inv.get("transaction_date")
    tax_details = get_tax_withholding_details(tax_withholding_category, posting_date, inv.company)
    if not tax_details:
        frappe.msgprint(
            _(
                "Skipping Tax Withholding Category {0} as there is no associated account set for Company {1} in it."
            ).format(tax_withholding_category, inv.company)
        )
        return {}, [], {}
    
    tax_amount, tax_deducted_on_advances, voucher_wise_amount = get_tax_amount(
        party_type, parties, inv, tax_details, posting_date, pan_no, net_amount
    )
    tax_row = get_tax_row_for_tds(tax_details, tax_amount)
    cost_center = get_cost_center(inv)
    tax_row.update({"cost_center": cost_center})
        
    return tax_row, tax_deducted_on_advances, voucher_wise_amount

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
            tax_amount = get_tds_amount(ldc, parties, inv, tax_details, vouchers, net_amount)

    if cint(tax_details.round_off_tax_amount):
        tax_amount = normal_round(tax_amount)

    return tax_amount, tax_deducted_on_advances, voucher_wise_amount


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
    if doctype != "Sales Invoice":
        pi = frappe.qb.DocType("Purchase Invoice").as_("pi")
        td = frappe.qb.DocType("Tax Withholding Detail").as_("td")
        additional_invoices_details = (
            frappe.qb.from_(td)
            .inner_join(pi)
            .on(pi.name == td.parent)
            .select(pi.name)
            .select(td.net_amount.as_("base_net_total"))
            .where(td.tax_withholding_category == tax_details.get("tax_withholding_category"))
            .where(pi.company == inv.company)
            .where(pi.supplier.isin(parties))
            .where(pi.is_opening == "No")
            .where(pi.docstatus == 1)
            .where(pi.posting_date.between(tax_details.from_date, tax_details.to_date))
            .run(as_dict=True)
        )
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
    pi = frappe.qb.DocType("Purchase Invoice").as_("pi")
    td = frappe.qb.DocType("Tax Withholding Detail").as_("td")
    item_wise_limit_consumed = (
        frappe.qb.from_(td)
        .inner_join(pi)
        .on(pi.name == td.parent)
        .select(Sum(td.net_amount).as_("amt"))
        .where(td.tax_withholding_category == ldc.tax_withholding_category)
        .where(pi.company == ldc.company)
        .where(pi.supplier.isin(parties))
        .where(pi.is_opening == "No")
        .where(pi.docstatus == 1)
        .where(pi.posting_date.between(ldc.valid_from, ldc.valid_upto))
    ).run(as_dict=True)
    limit_consumed += item_wise_limit_consumed[0].amt or 0

    return limit_consumed

def get_tds_amount(ldc, parties, inv, tax_details, vouchers, net_amount=0):
    tds_amount = 0
    invoice_filters = {"name": ("in", vouchers), "docstatus": 1, "apply_tds": 1, "tax_withholding_category": tax_details.get("tax_withholding_category")}

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
        invoice_filters.pop("tax_withholding_category", None)
        field = "sum(grand_total)"

        payment_entry_filters.pop("apply_tax_withholding_amount", None)
        payment_entry_filters.pop("tax_withholding_category", None)

    supp_credit_amt = frappe.db.get_value("Purchase Invoice", invoice_filters, field) or 0.0
    if not cint(tax_details.consider_party_ledger_amount):
        pi = frappe.qb.DocType("Purchase Invoice")
        td = frappe.qb.DocType("Tax Withholding Detail")
        item_wise_supp_credit_amt = (
            frappe.qb.from_(pi)
            .inner_join(td)
            .on(pi.name == td.parent)
            .select(Sum(td.net_amount).as_("amt"))
            .where(
                (pi.name.isin(vouchers or [""]))
                & (td.tax_withholding_category == tax_details.get("tax_withholding_category"))
                & (pi.apply_tds == 1)
                & (pi.docstatus == 1)
            )
        ).run(as_dict=True)
        
        supp_credit_amt += item_wise_supp_credit_amt[0].amt or 0

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
    if inv.item_wise_tds:
        supp_credit_amt += net_amount
    else:
        supp_credit_amt += inv.tax_withholding_net_total

    for type in payment_entry_amounts:
        if type.payment_type == "Pay":
            supp_credit_amt += type.amount
        else:
            supp_credit_amt -= type.amount
    threshold = tax_details.get("threshold", 0)
    cumulative_threshold = tax_details.get("cumulative_threshold", 0)

    if inv.doctype != "Payment Entry":
        if inv.item_wise_tds:
            tax_withholding_net_total = net_amount
        else:
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
            if not cint(tax_details.consider_party_ledger_amount):
                pi = frappe.qb.DocType("Purchase Invoice").as_("pi")
                td = frappe.qb.DocType("Tax Withholding Detail").as_("td")
                item_wise_net_total = (
                    frappe.qb.from_(pi)
                    .inner_join(td)
                    .on(pi.name == td.parent)
                    .select(Sum(td.net_amount).as_("amt"))
                    .where(
                        (pi.name.isin(vouchers or [""]))
                        & (pi.docstatus == 1)
                        & (pi.apply_tds == 1)
                        & (td.tax_withholding_category == tax_details.get("tax_withholding_category"))
                    )
                ).run(as_dict=True)
                net_total += item_wise_net_total[0].amt or 0
            if inv.item_wise_tds:
                net_total += net_amount
            else:
                net_total += inv.tax_withholding_net_total
            supp_credit_amt = net_total - cumulative_threshold

        if ldc and is_valid_certificate(ldc, inv.get("posting_date") or inv.get("transaction_date"), 0):
            tds_amount = get_lower_deduction_amount(
                supp_credit_amt, 0, ldc.certificate_limit, ldc.rate, tax_details
            )
        else:
            tds_amount = tax_withholding_net_total * tax_details.rate / 100 if supp_credit_amt > 0 else 0

    return tds_amount

