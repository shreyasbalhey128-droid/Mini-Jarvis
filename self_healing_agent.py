# self_healing_agent.py
import os
import re
import ast
import requests

class SelfHealingAgent:
    def __init__(self, ollama_url="http://127.0.0.1:11434", model="llama3.2:3b"):
        self.url = ollama_url
        self.model = model
        self.pending_patch = None  # Stores (target_file_path, original_code, patched_code)

    def generate_patch(self, error_msg, tb_text, file_path):
        """Analyzes an exception and generates a proposed code fix."""
        if not os.path.exists(file_path):
            return False, "Target file does not exist."

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code_content = f.read()
        except Exception as e:
            return False, f"Could not read source file: {e}"

        prompt = (
            f"You are an expert Python debugging assistant.\n"
            f"An exception occurred in the file '{os.path.basename(file_path)}'.\n\n"
            f"Error message:\n{error_msg}\n\n"
            f"Traceback:\n{tb_text}\n\n"
            f"Here is the current full code of the file:\n```python\n{code_content}\n```\n\n"
            f"Please fix the bug. Return ONLY the complete corrected Python code inside a single ```python code block. "
            f"Do not include any chat commentary or explanation outside the code block."
        )

        try:
            r = requests.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.1, "num_predict": 4096}
                },
                timeout=120
            )
            data = r.json()
            if "error" in data:
                return False, str(data["error"])

            raw_reply = data.get("message", {}).get("content", "").strip()

            # Clean markdown code blocks reliably
            code_match = re.search(r"```(?:python)?\s*\n(.*?)```", raw_reply, re.DOTALL)
            patched_code = code_match.group(1).strip() if code_match else raw_reply.strip()

            # Validate syntax before staging
            try:
                ast.parse(patched_code)
            except SyntaxError as se:
                return False, f"Generated patch contains syntax error: {se}"

            if not patched_code or patched_code == code_content.strip():
                return False, "Could not generate a meaningful fix."

            self.pending_patch = (file_path, code_content, patched_code)

            # Save proposed patch to a temporary file for review
            patch_file = file_path + ".proposed_patch.py"
            with open(patch_file, "w", encoding="utf-8") as f:
                f.write(patched_code)

            return True, patch_file

        except Exception as e:
            return False, str(e)

    def apply_pending_patch(self):
        """Applies the staged patch if confirmed by the user."""
        if not self.pending_patch:
            return False, "No pending patch to apply."

        file_path, original_code, patched_code = self.pending_patch

        try:
            # Backup original code before overwriting
            backup_file = file_path + ".bak"
            with open(backup_file, "w", encoding="utf-8") as f:
                f.write(original_code)

            # Write patched code
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(patched_code)

            # Clean up temp file
            patch_file = file_path + ".proposed_patch.py"
            if os.path.exists(patch_file):
                os.remove(patch_file)

            self.pending_patch = None
            return True, f"Successfully patched {os.path.basename(file_path)}. (Backup saved as {os.path.basename(backup_file)})"
        except Exception as e:
            return False, str(e)

    def discard_pending_patch(self):
        """Discards the staged patch."""
        if self.pending_patch:
            file_path = self.pending_patch[0]
            patch_file = file_path + ".proposed_patch.py"
            if os.path.exists(patch_file):
                os.remove(patch_file)
            self.pending_patch = None
        return "Patch discarded."