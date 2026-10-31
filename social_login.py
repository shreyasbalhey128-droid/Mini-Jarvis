"""
One-time login for Jarvis's WhatsApp + Instagram.

HOW TO USE
  1. Close Jarvis completely first (the browser profile can only be open in one
     place at a time, and Jarvis keeps it open for message alerts).
  2. Run this file (right-click > Run in PyCharm, or `python social_login.py`).
  3. Pick 1, 2 or 3. A visible browser opens.
       - WhatsApp: scan the QR (phone > Settings > Linked devices) and wait
         until your chats are on screen.
       - Instagram: log in, handle any "Save login info" / verification
         prompts, and wait until you see your feed or DMs.
  4. Come back to this window and press ENTER. That closes the browser
     cleanly, which is what saves the session.
  5. Start Jarvis again.

You only need to redo this if WhatsApp / Instagram log you out.
"""
from messaging_agent import SocialAgent


def main():
    agent = SocialAgent(headless=False)

    while True:
        print("\n=== Jarvis social login ===")
        print("1) WhatsApp")
        print("2) Instagram")
        print("3) Both (WhatsApp, then Instagram)")
        print("q) Quit")
        choice = input("Choose: ").strip().lower()

        if choice == "1":
            agent.setup_whatsapp_login()
        elif choice == "2":
            agent.setup_instagram_login()
        elif choice == "3":
            agent.setup_whatsapp_login()
            agent.setup_instagram_login()
        elif choice in ("q", "quit", "exit"):
            break
        else:
            print("Please type 1, 2, 3 or q.")
            continue

        print("\nDone. You can now close this and start Jarvis.")
        break


if __name__ == "__main__":
    main()