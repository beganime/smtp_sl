import os
import django
import re

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'smtp_sl.settings')
django.setup()

from mailer.models import Client 

def import_emails_from_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as file:
        lines = file.readlines()

    added_count = 0
    skipped_count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        match = re.search(r'(\d+)\.\s*([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', line)
        
        if match:
            client_number = match.group(1)
            email = match.group(2).lower()
            full_name = f"клиент # {client_number}"
            
            client, created = Client.objects.get_or_create(
                email=email,
                defaults={'full_name': full_name, 'is_active': True}
            )
            
            if created:
                added_count += 1
            else:
                skipped_count += 1
        else:
            print(f"Не удалось распознать строку: {line}")

    print(f"Готово! Добавлено новых клиентов: {added_count}. Пропущено (уже были в базе): {skipped_count}.")

if __name__ == '__main__':
    import_emails_from_file('emails.txt')
# python manage.py process_tasks    
