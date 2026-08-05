import requests

url = 'https://yitizehinliyashlar.com/'

for x in range(1000):
    response = requests.get(url)
    print(f"запрос #{x}")
