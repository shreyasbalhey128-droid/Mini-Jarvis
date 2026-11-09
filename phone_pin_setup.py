"""
Run this in a terminal to save (or remove) your phone's PIN / password for Jarvis:

    python phone_pin_setup.py            save or change it
    python phone_pin_setup.py --clear    delete it

It is typed here, hidden, and stored encrypted for your Windows account only (see phone_secret.py).
Pattern locks can't be used: Jarvis can only type a PIN or password.
"""
import getpass
import sys

import phone_secret


def main():
    if "--clear" in sys.argv:
        phone_secret.clear_pin()
        print("Saved phone PIN deleted.")
        return
    print("Type your phone's PIN or password. It is hidden while you type and stored encrypted for your Windows account.")
    pin = getpass.getpass("Phone PIN / password: ")
    if getpass.getpass("Type it again: ") != pin:
        print("They didn't match. Nothing saved.")
        return
    if not (4 <= len(pin) <= 32) or not pin.isascii():
        print("Use 4 to 32 plain English characters. Nothing saved.")
        return
    phone_secret.save_pin(pin)
    print("Saved. Restart Jarvis if it is running, then say: unlock my phone.")


if __name__ == "__main__":
    main()