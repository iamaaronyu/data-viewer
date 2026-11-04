"""Expose one RLDS episode as a timestep table in the existing viewer."""
import pyarrow as pa
from .arrow_src import ArrowSource
from .base import Source, SourceError
from ..embodied import Episode


class RLDSSource(ArrowSource):
    format='rlds'
    label='RLDS / TFRecord'

    def __init__(self,path,params,engine):
        Source.__init__(self,path,params,engine)
        try:
            self.episode=Episode(path,int(self.params.get('episode',0)))
        except (ValueError,KeyError,IndexError,OSError) as e:
            raise SourceError(str(e)) from e
        e=self.episode
        cols={'step':list(range(e.length))}
        cols.update({k:a.tolist() for k,a in e.arrays.items()})
        cols.update(e.images)
        if len(e.instructions)==e.length: cols['instruction']=e.instructions
        self.table=pa.table(cols); self._count=e.length

    def info(self): return self.episode.describe()

    def warnings(self): return self.episode.warnings
