# Standard Library
from contextlib import suppress
from functools import partial
from typing import cast

# GraphQL
import graphene_sqlalchemy
from graphene.utils.str_converters import to_snake_case
from promise import Promise, dataloader

# Database
from sqlalchemy import inspection
from sqlalchemy.orm import Load, aliased, contains_eager


MYPY = False
if MYPY:
    from typing import (
        Any,
        Callable,
        Dict,
        List,
        Optional,
        Tuple,
        Type,
        Union,
    )  # noqa: F401; pragma: no cover
    from graphql import ResolveInfo  # noqa: F401; pragma: no cover
    from graphene.relay import Connection  # noqa: F401; pragma: no cover
    from sqlalchemy.orm import Query  # noqa: F401; pragma: no cover
    from .filters import FilterSet  # noqa: F401; pragma: no cover


graphene_sqlalchemy_version_lt_2_1_2 = tuple(
    map(int, graphene_sqlalchemy.__version__.split('.'))
) < (2, 1, 2)


if graphene_sqlalchemy_version_lt_2_1_2:
    default_connection_field_factory = None  # pragma: no cover
else:
    from graphene_sqlalchemy.fields import default_connection_field_factory


DEFAULT_FILTER_ARG = 'filters'  # type: str


class ModelNotSupported(Exception):
    pass


class FilterableConnectionField(graphene_sqlalchemy.SQLAlchemyConnectionField):
    filter_arg = DEFAULT_FILTER_ARG  # type: str

    factory = None  # type: Union[FilterableFieldFactory, Callable, None]
    filters = {}  # type: dict

    def __init_subclass__(cls):
        if graphene_sqlalchemy_version_lt_2_1_2:
            return  # pragma: no cover

        if cls.filters and cls.factory is None:
            cls.factory = FilterableFieldFactory(cls.filters)

            if cls.filter_arg != DEFAULT_FILTER_ARG:
                # Update filter arg for nested fields.
                cls.factory.model_loader_class = type(
                    'CustomModelLoader',
                    (ModelLoader,),
                    {'filter_arg': cls.filter_arg},
                )
        elif cls.factory is None:
            cls.factory = default_connection_field_factory

    def __init__(self, connection, *args, **kwargs):
        if self.filter_arg not in kwargs:
            model = connection._meta.node._meta.model

            with suppress(KeyError):
                kwargs[self.filter_arg] = self.filters[model]

        super().__init__(connection, *args, **kwargs)

    @classmethod
    def get_query(cls, model, info: 'ResolveInfo', sort=None, **args):
        """Standard get_query with filtering."""
        query = super().get_query(model, info, sort, **args)

        request_filters = args.get(cls.filter_arg)
        if request_filters:
            filter_set = cls.get_filter_set(info)
            query = filter_set.filter(info, query, request_filters)

        return query

    @classmethod
    def get_filter_set(cls, info: 'ResolveInfo') -> 'FilterSet':
        """
        Get field filter set.

        Args:
            info: Graphene resolve info object.

        Returns:
            FilterSet class from field args.

        """
        field_name = info.field_asts[0].name.value
        schema_field = info.parent_type.fields.get(field_name)
        filters_type = schema_field.args[cls.filter_arg].type
        filters = filters_type.graphene_type  # type: FilterSet
        return filters


class ModelLoader(dataloader.DataLoader):
    filter_arg = DEFAULT_FILTER_ARG  # type: str

    def __init__(
        self,
        parent_model: 'Any',
        model: 'Any',
        info: 'ResolveInfo',
        graphql_args: dict,
    ):
        """
        Dataloader for SQLAlchemy model relations.

        Args:
            parent_model: Parent SQLAlchemy model.
            model: SQLAlchemy model.
            info: Graphene resolve info object.
            graphql_args: Request args: filters, sort, ...

        """
        super().__init__()
        self.info = info  # type: ResolveInfo
        self.graphql_args = graphql_args  # type: dict

        self.model = model # type: Any
        self.parent_model = parent_model  # type: Any
        self.parent_model_pk_field = self._get_model_pk_field_name(
            self.parent_model
        )  # type: str

        self.model_relation_field = to_snake_case(self.info.field_name)  # type: str

        self.relation = getattr(
            self.parent_model, self.model_relation_field
        )  # type: Any

    def batch_load_fn(self, keys: 'List[Any]') -> Promise:
        """
        Load related objects.

        Args:
            keys: Primary key values of parent model.

        Returns:
            Lists of related orm objects.

        """
        query = self._get_query().filter(
            getattr(self.parent_model, self.parent_model_pk_field).in_(keys)
        )  # type: Query

        objects = {
            self.parent_model_object_to_key(parent_object): getattr(
                parent_object, self.model_relation_field
            )
            for parent_object in query
        } # type: Dict[tuple, Any]
        return Promise.resolve(
            [objects.get(object_id, []) for object_id in keys]
        )

    @staticmethod
    def _get_model_pk_field_name(model) -> str:
        """
        Get primary key field name.

        Args:
            model: SQLAlchemy model.

        Returns:
            Field name.

        """
        model_pk_fields = tuple(
            (
                cast(str, name)
                for name, c in inspection.inspect(model).columns.items()
                if c.primary_key
            )
        ) # type: Tuple[str]
        if len(model_pk_fields) != 1:
            raise ModelNotSupported(
                'The number of primary keys must be equal to 1 '
                'but {} were given. Model: {}.'.format(
                    len(model_pk_fields), model
                )
            )

        model_pk_field = model_pk_fields[0]  # type: str
        return model_pk_field

    def parent_model_object_to_key(self, parent_object: 'Any') -> 'Any':
        """
        Get primary key value from SQLAlchemy orm object.

        Args:
            parent_object: SQLAlchemy orm object.

        Returns:
            Primary key value.

        """
        return getattr(parent_object, self.parent_model_pk_field)

    @classmethod
    def _get_filter_set(cls, info: 'ResolveInfo') -> 'FilterSet':
        """
        Get field filter set.

        Args:
            info: Graphene resolve info object.

        Returns:
            FilterSet class from field args.

        """
        field_name = info.field_asts[0].name.value
        schema_field = info.parent_type.fields.get(field_name)
        filters_type = schema_field.args[cls.filter_arg].type
        filters = filters_type.graphene_type  # type: FilterSet
        return filters

    def _get_query(self) -> 'Query':
        """
        Build, filter and sort the query.

        Returns:
            SQLAlchemy query.

        """
        subquery = graphene_sqlalchemy.get_query(self.model, self.info.context)

        request_filters = self.graphql_args.get(self.filter_arg)
        if request_filters:
            filter_set = self._get_filter_set(self.info)
            subquery = filter_set.filter(self.info, subquery, request_filters)

        sort = self.graphql_args.get('sort')
        if sort and isinstance(sort, list):
            subquery = subquery.order_by(*(col.value for col in sort))

        aliased_model = aliased(self.model, subquery.subquery())

        query = (
            graphene_sqlalchemy.get_query(self.parent_model, self.info.context)
            .join(aliased_model, self.relation)
            .options(
                contains_eager(self.relation, alias=aliased_model),
                Load(self.parent_model).load_only(self.parent_model_pk_field),
            )
        )

        return query


class NestedFilterableConnectionField(FilterableConnectionField):
    dataloaders_field = '_sqla_filter_dataloaders'  # type: str

    @classmethod
    def _get_or_create_data_loader(
        cls, root: 'Any', model: 'Any', info: 'ResolveInfo', args: dict
    ) -> ModelLoader:
        """
        Get or create (and save) dataloader from ResolveInfo

        Args:
            root: Parent model orm object.
            model: SQLAlchemy model.
            info: Graphene resolve info object.
            args: Request args: filters, sort, ...

        Returns:
            Dataloader for SQLAlchemy model.

        """
        context = info.context  # type: Union[dict, object]

        if isinstance(context, dict):
            try:
                data_loaders = context[cls.dataloaders_field]
            except KeyError:
                data_loaders = {}
                context[cls.dataloaders_field] = data_loaders

        else:
            data_loaders = getattr(context, cls.dataloaders_field, None)
            if data_loaders is None:
                data_loaders = {}
                setattr(info.context, cls.dataloaders_field, data_loaders)

        # Unique dataloader key for context.
        data_loader_key = tuple((p for p in info.path if isinstance(p, str)))

        try:
            current_data_loader = data_loaders[data_loader_key]  # type: ModelLoader
        except KeyError:
            current_data_loader = ModelLoader(type(root), model, info, args)
            data_loaders[data_loader_key] = current_data_loader

        return current_data_loader

    @classmethod
    def connection_resolver(
        cls,
        resolver: 'Any',
        connection_type: 'Any',
        model: 'Any',
        root: 'Any',
        info: 'ResolveInfo',
        **kwargs: dict
    ) -> 'Union[Promise, Connection]':
        """
        Resolve nested connection.

        Args:
            resolver: Default resolver.
            connection_type: Connection class.
            model: SQLAlchemy model.
            root: Parent SQLAlchemy object.
            info: Graphene resolve info object.
            **kwargs: Request args: filters, sort, ...

        Returns:
            Connection object.

        """
        data_loader = cls._get_or_create_data_loader(
            root, model, info, kwargs
        )  # type: ModelLoader
        root_pk_value = data_loader.parent_model_object_to_key(root)  # type: tuple
        resolved = data_loader.load(root_pk_value)  # type: Promise

        on_resolve = partial(
            cls.resolve_connection, connection_type, model, info, kwargs
        )
        return Promise.resolve(resolved).then(on_resolve)


class FilterableFieldFactory:
    model_loader_class = ModelLoader  # type: Type[ModelLoader]
    field_class = (
        NestedFilterableConnectionField
    )  # type: Type[NestedFilterableConnectionField]

    def __init__(self, model_filters: dict):
        self.model_filters = model_filters

    def __call__(
        self, relationship: 'Any', registry: 'Any' = None, **field_kwargs: dict
    ) -> NestedFilterableConnectionField:
        """
        Get field for relation.

        Args:
            relationship: SQLAlchemy relation.
            registry: graphene-sqlalchemy registry.
            **field_kwargs: Field args.

        Returns:
            Filed object.

        """
        model = relationship.mapper.entity
        model_type = registry.get_type_for_model(model)

        filters = self.model_filters.get(model)  # type: Optional[FilterSet]

        if filters is not None:
            field_kwargs.setdefault(
                self.model_loader_class.filter_arg, filters
            )

        return self.field_class(model_type._meta.connection, **field_kwargs)
