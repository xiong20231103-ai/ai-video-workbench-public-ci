import json
import os

# List of languages to be translated
languages = {
    'fr': 'Français (French)',
    'de': 'Deutsch (German)',
    'es': 'Español (Spanish)',
    'it': 'Italiano (Italian)',
    'ja': '日本語 (Japanese)',
    'ko': '한국어 (Korean)',
    'ru': 'Русский (Russian)',
    'pt': 'Português (Portuguese)',
    'nl': 'Nederlands (Dutch)',
    'ar': 'العربية (Arabic)',
    'hi': 'हिन्दी (Hindi)',
    'tr': 'Türkçe (Turkish)',
    'vi': 'Tiếng Việt (Vietnamese)',
    'th': 'ไทย (Thai)'
}

# Read the English translation file
with open("en.json", "r", encoding="utf-8") as f:
    en_data = json.load(f)

# Make sure the output directory exists
output_dir = "translations"
os.makedirs(output_dir, exist_ok=True)

# Generate translations for each language
for lang_code, lang_name in languages.items():
    translated_data = en_data.copy()
    translated_data["language_info"]["name"] = lang_name.split(" (")[0]
    translated_data["language_info"]["native_name"] = lang_name.split(" (")[0]

    output_file = os.path.join(output_dir, f"{lang_code}.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(translated_data, f, indent=2, ensure_ascii=False)

    print(f"✅ {output_file} created!")

print("🎉 All translation files have been generated successfully!")
