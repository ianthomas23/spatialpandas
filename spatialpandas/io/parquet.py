import copy
import json
import pathlib
from functools import reduce

import pandas as pd
from dask import delayed
from dask.dataframe import (
    to_parquet as dd_to_parquet, from_delayed, from_pandas
)
from dask.dataframe.utils import make_meta, clear_known_categories

from pandas.io.parquet import (
    to_parquet as pd_to_parquet,
)

import pyarrow as pa
from pyarrow import parquet as pq
from spatialpandas.io.utils import validate_coerce_filesystem
from spatialpandas import GeoDataFrame
from spatialpandas.dask import DaskGeoDataFrame
from spatialpandas.geometry.base import to_geometry_array
from spatialpandas.geometry import (
    PointDtype, MultiPointDtype, RingDtype, LineDtype,
    MultiLineDtype, PolygonDtype, MultiPolygonDtype, GeometryDtype
)

_geometry_dtypes = [
    PointDtype, MultiPointDtype, RingDtype, LineDtype,
    MultiLineDtype, PolygonDtype, MultiPolygonDtype
]


def _import_geometry_columns(df, geom_cols):
    new_cols = {}
    for col, type_str in geom_cols.items():
        if col in df and not isinstance(df.dtypes[col], GeometryDtype):
            new_cols[col] = to_geometry_array(df[col], dtype=type_str)

    return df.assign(**new_cols)


def _load_parquet_pandas_metadata(path, filesystem=None):
    filesystem = validate_coerce_filesystem(path, filesystem)
    if not filesystem.exists(path):
        raise ValueError("Path not found: " + path)

    if filesystem.isdir(path):
        pqds = pq.ParquetDataset(
            path, filesystem=filesystem, validate_schema=False
        )
        common_metadata = pqds.common_metadata
        if common_metadata is None:
            # Get metadata for first piece
            piece = pqds.pieces[0]
            metadata = piece.get_metadata().metadata
        else:
            metadata = pqds.common_metadata.metadata
    else:
        with filesystem.open(path) as f:
            pf = pq.ParquetFile(f)
        metadata = pf.metadata.metadata

    return json.loads(
        metadata.get(b'pandas', b'{}').decode('utf')
    )


def _get_geometry_columns(pandas_metadata):
    columns = pandas_metadata.get('columns', [])
    geom_cols = {}
    for col in columns:
        type_string = col.get('numpy_type', None)
        is_geom_col = False
        for geom_type in _geometry_dtypes:
            try:
                geom_type.construct_from_string(type_string)
                is_geom_col = True
            except TypeError:
                pass
        if is_geom_col:
            geom_cols[col["name"]] = col["numpy_type"]

    return geom_cols


def to_parquet(
    df,
    fname,
    compression="snappy",
    index=None,
    **kwargs
):
    # Standard pandas to_parquet with pyarrow engine
    pd_to_parquet(
        df, fname, engine="pyarrow", compression=compression, index=index, **kwargs
    )


def read_parquet(path, columns=None, filesystem=None):
    filesystem = validate_coerce_filesystem(path, filesystem)

    # Load using pyarrow to handle parquet files and directories across filesystems
    df = pq.ParquetDataset(
        path, filesystem=filesystem, validate_schema=False
    ).read(columns=columns).to_pandas()

    # Import geometry columns, not needed for pyarrow >= 0.16
    metadata = _load_parquet_pandas_metadata(path, filesystem=filesystem)
    geom_cols = _get_geometry_columns(metadata)
    if geom_cols:
        df = _import_geometry_columns(df, geom_cols)

    # Return result
    return GeoDataFrame(df)


def to_parquet_dask(
        ddf, path, compression="snappy", filesystem=None, storage_options=None, **kwargs
):
    assert isinstance(ddf, DaskGeoDataFrame)
    filesystem = validate_coerce_filesystem(path, filesystem)
    if path and filesystem.isdir(path):
        filesystem.rm(path, recursive=True)

    dd_to_parquet(
        ddf, path, engine="pyarrow", compression=compression,
        storage_options=storage_options, **kwargs
    )

    # Write partition bounding boxes to the _metadata file
    partition_bounds = {}
    for series_name in ddf.columns:
        series = ddf[series_name]
        if isinstance(series.dtype, GeometryDtype):
            if series._partition_bounds is None:
                # Bounds are not already computed. Compute bounds from the parquet file
                # that was just written.
                filesystem.invalidate_cache(path)
                series = read_parquet_dask(
                    path,
                    columns=[series_name],
                    filesystem=filesystem,
                    storage_options=storage_options,
                    load_divisions=False
                )[series_name]
            partition_bounds[series_name] = series.partition_bounds.to_dict()

    spatial_metadata = {'partition_bounds': partition_bounds}
    b_spatial_metadata = json.dumps(spatial_metadata).encode('utf')

    pqds = pq.ParquetDataset(path, filesystem=filesystem, validate_schema=False)
    all_metadata = copy.copy(pqds.common_metadata.metadata)
    all_metadata[b'spatialpandas'] = b_spatial_metadata
    schema = pqds.common_metadata.schema.to_arrow_schema()
    new_schema = schema.with_metadata(all_metadata)
    with filesystem.open(pqds.common_metadata_path, 'wb') as f:
        pq.write_metadata(new_schema, f)


def read_parquet_dask(
        path, columns=None, filesystem=None, load_divisions=False,
        geometry=None, bounds=None, **kwargs
):
    """
    Read spatialpandas parquet dataset(s) as DaskGeoDataFrame. Datasets are assumed to
    have been written with the DaskGeoDataFrame.to_parquet or
    DaskGeoDataFrame.pack_partitions_to_parquet methods.

    Args:
        path: Path to spatialpandas parquet dataset, or list of paths to datasets, or
            glob string referencing one or more parquet datasets.
        columns: List of columns to load
        filesystem: fsspec filesystem to use to read datasets
        load_divisions: If True, attempt to load the hilbert_distance divisions for
            each partition.  Only available for datasets written using the
            pack_partitions_to_parquet method.
        geometry: The name of the column to use as the geometry column of the
            resulting DaskGeoDataFrame. Defaults to the first geometry column in the
            dataset.
        bounds: If specified, only load partitions that have the potential to intersect
            with the provided bounding box coordinates. bounds is a length-4 tuple of
            the form (xmin, ymin, xmax, ymax).
    Returns:
    DaskGeoDataFrame
    """
    # Normalize path to a list
    if isinstance(path, (str, pathlib.Path)):
        path = [str(path)]
    else:
        path = list(path)
        if not path:
            raise ValueError('Empty path specification')

    # Infer filesystem
    filesystem = validate_coerce_filesystem(path[0], filesystem)

    # Expand glob
    if len(path) == 1 and ('*' in path[0] or '?' in path[0] or '[' in path[0]):
        path = filesystem.glob(path[0])

    # Perform read parquet
    result = _perform_read_parquet_dask(
        path, columns, filesystem,
        load_divisions=load_divisions, geometry=geometry, bounds=bounds
    )

    return result


def _perform_read_parquet_dask(
        paths, columns, filesystem, load_divisions, geometry=None, bounds=None
):
    filesystem = validate_coerce_filesystem(paths[0], filesystem)
    datasets = [pa.parquet.ParquetDataset(
        path, filesystem=filesystem, validate_schema=False
    ) for path in paths]

    # Create delayed partition for each piece
    pieces = [piece for dataset in datasets for piece in dataset.pieces]
    delayed_partitions = [
        delayed(read_parquet)(
            piece.path, columns=columns, filesystem=filesystem
        )
        for piece in pieces
    ]

    # Load divisions
    if load_divisions:
        div_mins_list, div_maxes_list = zip(*[
            _load_divisions(dataset) for dataset in datasets
        ])

        div_mins = reduce(lambda a, b: a + b, div_mins_list, [])
        div_maxes = reduce(lambda a, b: a + b, div_maxes_list, [])
    else:
        div_mins = None
        div_maxes = None

    # load partition bounds
    partition_bounds_list = [_load_partition_bounds(dataset) for dataset in datasets]
    if not any([b is None for b in partition_bounds_list]):
        partition_bounds = {}
        # We have partition bounds for all datasets
        for partition_bounds_el in partition_bounds_list:
            for col, col_bounds in partition_bounds_el.items():
                col_bounds_list = partition_bounds.get(col, [])
                col_bounds_list.append(col_bounds)
                partition_bounds[col] = col_bounds_list

        # Concat bounds for each geometry column
        for col in list(partition_bounds):
            partition_bounds[col] = pd.concat(
                partition_bounds[col], axis=0
            ).reset_index(drop=True)
            partition_bounds[col].index.name = 'partition'
    else:
        partition_bounds = {}

    # Create meta DataFrame
    # Make categories unknown because we can't be sure all categories are present in
    # the first partition.
    meta = delayed(make_meta)(delayed_partitions[0]).compute()
    meta = clear_known_categories(meta)

    # Handle geometry
    if geometry:
        meta = meta.set_geometry(geometry)

    geometry = meta.geometry.name

    # Filter partitions by bounding box
    if bounds and geometry in partition_bounds:
        # Unpack bounds coordinates and make sure
        x0, y0, x1, y1 = bounds

        # Make sure x0 < c1
        if x0 > x1:
            x0, x1 = x1, x0

        # Make sure y0 < y1
        if y0 > y1:
            y0, y1 = y1, y0

        # Make DataFrame with bounds and parquet piece
        partitions_df = partition_bounds[geometry].assign(
            delayed_partition=delayed_partitions
        )

        if load_divisions:
            partitions_df = partitions_df.assign(div_mins=div_mins, div_maxes=div_maxes)

        inds = ~(
            (partitions_df.x1 < x0) |
            (partitions_df.y1 < y0) |
            (partitions_df.x0 > x1) |
            (partitions_df.y0 > y1)
        )

        partitions_df = partitions_df[inds]
        for col in list(partition_bounds):
            partition_bounds[col] = partition_bounds[col][inds]
            partition_bounds[col].reset_index(drop=True, inplace=True)
            partition_bounds[col].index.name = "partition"

        delayed_partitions = partitions_df.delayed_partition.tolist()
        if load_divisions:
            div_mins = partitions_df.div_mins
            div_maxes = partitions_df.div_maxes

    if load_divisions:
        divisions = div_mins + [div_maxes[-1]]
        if divisions != sorted(divisions):
            raise ValueError(
                "Cannot load divisions because the discovered divisions are unsorted.\n"
                "Set load_divisions=False to skip loading divisions."
            )
    else:
        divisions = None

    # Create DaskGeoDataFrame
    if delayed_partitions:
        result = from_delayed(delayed_partitions, divisions=divisions, meta=meta)
    else:
        # Single partition empty result
        result = from_pandas(meta, npartitions=1)

    # Set partition bounds
    if partition_bounds:
        result._partition_bounds = partition_bounds

    return result


def _load_partition_bounds(pqds):
    partition_bounds = None
    if (pqds.common_metadata is not None and
            b'spatialpandas' in pqds.common_metadata.metadata):
        spatial_metadata = json.loads(
            pqds.common_metadata.metadata[b'spatialpandas'].decode('utf')
        )
        if "partition_bounds" in spatial_metadata:
            partition_bounds = {}
            for name in spatial_metadata['partition_bounds']:
                bounds_df = pd.DataFrame(
                    spatial_metadata['partition_bounds'][name]
                )

                # Index labels will be read in as strings.
                # Here we convert to integers, sort by index, then drop index just in
                # case the rows got shuffled on read
                bounds_df = (bounds_df
                             .set_index(bounds_df.index.astype('int'))
                             .sort_index()
                             .reset_index(drop=True))
                bounds_df.index.name = 'partition'

                partition_bounds[name] = bounds_df

    return partition_bounds


def _load_divisions(pqds):
    fmd = pqds.metadata
    row_groups = [fmd.row_group(i) for i in range(fmd.num_row_groups)]
    rg0 = row_groups[0]
    div_col = None
    for c in range(rg0.num_columns):
        if rg0.column(c).path_in_schema == "hilbert_distance":
            div_col = c
            break

    if div_col is None:
        # No hilbert_distance column found
        raise ValueError(
            "Cannot load divisions because hilbert_distance index not found"
        )

    mins, maxes = zip(*[
        (rg.column(div_col).statistics.min, rg.column(12).statistics.max)
        for rg in row_groups
    ])

    return mins, maxes
