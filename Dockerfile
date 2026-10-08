FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ${LAMBDA_TASK_ROOT}/
COPY static ${LAMBDA_TASK_ROOT}/static
COPY data/fraud.db ${LAMBDA_TASK_ROOT}/data/fraud.db

CMD ["app.handler"]
