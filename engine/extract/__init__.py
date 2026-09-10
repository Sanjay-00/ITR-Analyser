"""
engine.extract  -  read what a document actually says, without interpreting it.

    itr_parser   format-independent primitives: Indian-format number parsing
                 (to_int), and identity extraction (PAN / AY / name / form)
    financials   locates Balance Sheet / P&L blocks in a page's rows, harvests
                 their line items, and verifies each against its own printed
                 totals - the arithmetic proof the rest of the pipeline trusts
"""
