"""Public page inspection requires no workspace key or account cookie.

Set AVAILKIT_PAGE_ID to a published page, then run python public_booking.py.
This read-only example does not create or print private booking capabilities.
"""

import json
import os
import sys

from availkit import AvailKitError, PublicBookingClient


def main() -> None:
    try:
        client = PublicBookingClient(page_id=os.environ["AVAILKIT_PAGE_ID"],
            base_url=os.environ.get("AVAILKIT_BASE_URL", "https://api.availkit.com"))
        page = client.get_page()
        offerings = client.list_offerings()
        print(json.dumps({"pageId": page["id"], "environment": page["environment"],
                          "offeringCount": len(offerings["items"])}))
    except AvailKitError as error:
        print(json.dumps(error.as_dict()), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('{"code":"example_failed"}', file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
