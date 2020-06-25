"""All the flask api endpoints."""
import csv
import io
import logging
import typing as T
from collections import Counter
from pathlib import Path

from flask import current_app
from flask import Flask
from flask_restful import Api  # type: ignore
from flask_restful import reqparse
from flask_restful import Resource
from sklearn import model_selection
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import BadRequest
from werkzeug.exceptions import HTTPException
from werkzeug.exceptions import NotFound

from flask_app import db
from flask_app import utils
from flask_app.modeling.train_queue import ModelScheduler

API_URL_PREFIX = "/api"

logger = logging.getLogger(__name__)
# mypy doesn't support recrsive types, so this is the best we can do
Json = T.Optional[T.Union[T.List[T.Any], T.Dict[str, T.Any], int, str, bool]]


class UnprocessableEntity(HTTPException):
    """."""

    code = 422
    description = "The entity supplied has errors and cannot be processed."


class AlreadyExists(HTTPException):
    """."""

    code = 403
    description = "The resource already exists."


class BaseResource(Resource):
    """Every resource derives from this.

    Attributes:
        url:
    """

    url: str

    @staticmethod
    def _write_headers_and_data_to_csv(
        headers: T.List[str], data: T.List[T.List[str]], csvfile: Path
    ) -> None:

        with csvfile.open("w") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(data)


class ClassifierRelatedResource(BaseResource):
    """Base class to define utility functions related to classifiers."""

    @staticmethod
    def _classifier_status(clsf: db.Classifier) -> Json:
        """Process a Classifier instance and format it into the API spec.

        Returns:
            {
                "classifier_id": int,
                "name": str,
                "trained_by_openFraming": bool,
                "category_names": T.List[str],
                "training_status": T.Union["not_begun", "training", "completed"]
          }
        """
        if clsf.train_set is None:
            training_status = "not_begun"
        else:
            if clsf.train_set.training_or_inference_completed:
                training_status = "completed"
            else:
                training_status = "training"

        category_names = clsf.category_names

        return {
            "classifier_id": clsf.classifier_id,
            "name": clsf.name,
            "trained_by_openFraming": clsf.trained_by_openFraming,
            "category_names": category_names,
            "training_status": training_status,
        }


class Classifiers(ClassifierRelatedResource):
    """Create a classifer, get a list of classifiers."""

    url = "/classifiers"

    def __init__(self) -> None:
        """Set up request parser."""
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument(name="name", type=str, required=True)

        def category_names_type(val: T.Any) -> str:
            if not isinstance(val, str):
                raise ValueError("must be str")
            if "," in val:
                raise ValueError("can't contain commas.")

            return val

        self.reqparse.add_argument(
            name="category_names",
            type=category_names_type,
            action="append",
            required=True,
            help="",
        )

    def post(self) -> Json:
        """Create a classifier.

        req_body:
            json:
                {
                    "name": str,
                    "category_names": T.List[str]
                }

        Returns:
            {
                "classifier_id": int,
                "name": str,
                "trained_by_openFraming": bool,
                "status": "not_begun",
                "category_names": T.List[str],
            }
        """
        args = self.reqparse.parse_args()
        if (
            len(args["category_names"]) < 2
        ):  # I don't know how to do this validation in the
            # RequestParser
            raise BadRequest("must be at least two categories.")

        category_names = args["category_names"]
        name = args["name"]
        # Use a placeholder for file_path to get the auto incremented id
        clsf = db.Classifier.create(
            name=name, category_names=category_names, dir_path="WILL_BE_REPLACED"
        )
        clsf.save()

        dir_ = utils.Files.classifier_dir(
            classifier_id=clsf.classifier_id, ensure_exists=True
        )
        clsf.dir_path = str(dir_.resolve())
        return self._classifier_status(clsf)

    def get(self) -> Json:
        """Get a list of classifiers.

        Returns:
            [
              {
                "classifier_id": int,
                "name": str,
                "trained_by_openFraming": bool,
                "status": T.Union["not_begun", "training", "completed"]
                "category_names": T.List[str],
              },
              ...
            ]
        """
        res: T.List[Json] = [
            self._classifier_status(clsf) for clsf in db.Classifier.select()
        ]
        return res


class ClassifiersTrainingFile(ClassifierRelatedResource):
    """Upload training data to the classifier."""

    url = "/classifiers/<int:classifier_id>/training/file"

    def __init__(self) -> None:
        """Set up request parser."""
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument(
            name="file", type=FileStorage, required=True, location="files"
        )

    def post(self, classifier_id: int) -> Json:
        """Upload a training set for classifier, and start training.

        Body:
            FormData: with "file" item.

        Returns:
            {
                "classifier_id": int,
                "name": str,
                "trained_by_openFraming": bool,
                "category_names": T.List[str],
                "training_status": "training"
            }

        Raises:
            BadRequest
            UnprocessableEntity

        """
        args = self.reqparse.parse_args()
        file_: FileStorage = args["file"]

        try:
            classifier = db.Classifier.get(db.Classifier.classifier_id == classifier_id)
        except db.Classifier.DoesNotExist:
            raise NotFound("classifier not found.")

        if classifier.train_set is not None:
            raise AlreadyExists("This classifier already has a training set.")

        table_headers, table_data = self._validate_training_file_and_get_data(
            classifier.category_names, file_
        )
        # Split into train and dev
        ss = model_selection.StratifiedShuffleSplit(n_splits=1, test_size=0.2)
        X, y = zip(*table_data)
        train_indices, dev_indices = next(ss.split(X, y))

        train_data = [table_data[i] for i in train_indices]
        dev_data = [table_data[i] for i in dev_indices]

        train_file = utils.Files.classifier_train_set_file(classifier_id)
        self._write_headers_and_data_to_csv(table_headers, train_data, train_file)
        dev_file = utils.Files.classifier_dev_set_file(classifier_id)
        self._write_headers_and_data_to_csv(table_headers, dev_data, dev_file)

        classifier.train_set = db.LabeledSet()
        classifier.dev_set = db.LabeledSet()
        classifier.train_set.save()
        classifier.dev_set.save()
        classifier.save()

        # Refresh classifier
        classifier = db.Classifier.get(db.Classifier.classifier_id == classifier_id)

        model_scheduler: ModelScheduler = current_app.config["MODEL_SCHEDULER"]

        # TODO: Add a check to make sure model training didn't start already and crashed

        model_scheduler.add_training_process(
            labels=classifier.category_names,
            model_path=utils.TRANSFORMERS_MODEL,
            data_dir=str(utils.Files.classifier_dir(classifier_id)),
            cache_dir=current_app.config["TRANSFORMERS_CACHE_DIR"],
            output_dir=str(
                utils.Files.classifier_output_dir(classifier_id, ensure_exists=True)
            ),
        )

        return self._classifier_status(classifier)

    @staticmethod
    def _validate_training_file_and_get_data(
        category_names: T.List[str], file_: FileStorage
    ) -> T.Tuple[T.List[str], T.List[T.List[str]]]:
        """Validate user input and return uploaded CSV data, without the headers.

        Args:
            category_names: The categories for the classifier.
            file_: uploaded file.

        Returns:
            table_headers: A list of length 2.
            table_data: A list of lists of length 2.
        """
        # TODO: Write tests for all of these!

        table = utils.Validate.csv_and_get_table(T.cast(io.BytesIO, file_))

        utils.Validate.table_has_num_columns(table, 2)
        utils.Validate.table_has_headers(table, ["example", "category"])

        table_headers, table_data = table[0], table[1:]

        min_num_examples = int(len(table_data) * utils.TEST_SET_SPLIT)
        if len(table_data) < min_num_examples:
            raise BadRequest(
                f"We need at least {min_num_examples} labelled examples for this issue."
            )

        # TODO: Low priority: Make this more efficient.
        category_names_counter = Counter(category for _, category in table_data)

        unique_category_names = category_names_counter.keys()
        if set(category_names) != unique_category_names:
            # TODO: Lower case category names before checking.
            # TODO: More helpful error messages when there is an error with the
            # the categories in an uploaded training file.
            raise UnprocessableEntity(
                "The categories for this classifier are"
                f" {category_names}. But the uploaded file either"
                " has some categories missing, or has categories in addition to the"
                " ones indicated."
            )

        categories_with_less_than_two_exs = [
            category for category, count in category_names_counter.items() if count < 2
        ]
        if categories_with_less_than_two_exs:
            raise UnprocessableEntity(
                "There are less than two examples with the categories: "
                f"{','.join(categories_with_less_than_two_exs)}."
                " We need at least two examples per category."
            )

        return table_headers, table_data


def create_app(
    project_data_dir: Path = Path("./project_data"),
    transformers_cache_dir: Path = Path("./transformers_cache_dir"),
) -> Flask:
    """App factory to for easier testing.

    Args:
        project_data_dir: If None, will be read from the PROJECT_DATA_DIR environment
            variable, or will be set to ./project_data.

    Sets:
        app.config["PROJECT_DATA_DIR"]
        app.config["TRANSFORMERS_CACHE_DIR"]
        app.config["MODEL_SCHEDULER"]

    Returns:
        app: Flask() object.
    """
    app = Flask(__name__, static_url_path="/", static_folder="../frontend")

    @app.before_request
    def _db_connect() -> None:
        """Ensures that a connection is opened to handle queries by the request."""
        db.DATABASE.connect()

    @app.teardown_request
    def _db_close(exc: T.Optional[Exception]) -> None:
        """Close on tear down."""
        if not db.DATABASE.is_closed():
            db.DATABASE.close()

    app.config["PROJECT_DATA_DIR"] = project_data_dir
    app.config["MODEL_SCHEDULER"] = ModelScheduler()
    app.config["TRANSFORMERS_CACHE_DIR"] = transformers_cache_dir

    api = Api(app)
    # `utils.Files` uses flask.current_app. Since we're not
    # handling a request just yet, we need this.
    with app.app_context():
        # Create the project data directory
        # In the future, this hould be disabled.
        utils.Files.project_data_dir(ensure_exists=True)
        utils.Files.supervised_dir(ensure_exists=True)
        utils.Files.unsupervised_dir(ensure_exists=True)

    lsresource_cls: T.Tuple[T.Type[BaseResource], ...] = (
        Classifiers,
        ClassifiersTrainingFile,
    )
    for resource_cls in lsresource_cls:
        assert (
            resource_cls.url[0] == "/"
        ), f"{resource_cls.__name__}.url must start with a /"
        url = API_URL_PREFIX + resource_cls.url
        api.add_resource(resource_cls, url)

    return app
