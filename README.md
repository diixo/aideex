# aideex

ASPICE assessment/compliance checker for software development projects.


## Run django-server:
```bash
py ui/manage.py runserver
```


### Run Backend server:
Run from root directory:

``` bash
py -3.10 .\scripts\run_backend_server.py
```


### Fast-API documentation:
`http://127.0.0.1:8001/docs`



## Create repository structure

```bash
django-admin startproject ui
```

```bash
cd ui
```

```bash
py manage.py startapp app_main
```

- DB migrations with migrates:
```bash
py ui/manage.py makemigrations
py ui/manage.py migrate
```

