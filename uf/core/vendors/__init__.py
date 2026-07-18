"""uglyfruit vendor modules — one per platform. Each owns its discriminators,
its structural/error helpers, its EOS-or-other->contract translators, its
fixtures, and an ARISTA_DISCRIMINATORS-style (vendor, cap) -> fn map that the
core registry assembles. Add a vendor = add a module here + one import line in
reading.py (Design Note 05 §2a)."""