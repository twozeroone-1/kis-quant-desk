import sys

sys.path.extend([".", "domestic_stock"])

import kis_auth as ka
from domestic_stock_functions import inquire_price


def main() -> None:
    ka.auth(svr="vps")
    df = inquire_price("demo", "J", "005930")
    print(df[["stck_prpr", "prdy_vrss", "prdy_ctrt", "acml_vol"]].head(1).to_string(index=False))


if __name__ == "__main__":
    main()
