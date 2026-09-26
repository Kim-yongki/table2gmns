import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import wkt
from shapely.geometry import LineString, MultiLineString, Point, Polygon

import table2gmns as sg


def line(x1=0, x2=100, y=0):
    return LineString([(300000 + x1, 4400000 + y), (300000 + x2, 4400000 + y)])


def data(geometries=None, **columns):
    return gpd.GeoDataFrame(columns, geometry=geometries or [line()], crs=26917)


def convert(frame, **kwargs):
    fields = {"geometry": "geometry", **kwargs.pop("link_field_map", {})}
    return sg.getNetFromFile(frame, link_field_map=fields, **kwargs)


class ConversionTests(unittest.TestCase):
    def test_mapping_is_required(self):
        with self.assertRaises(TypeError):
            sg.getNetFromFile(data(), default_directed=0)
        with self.assertRaisesRegex(ValueError,'explicitly map geometry'):
            sg.getNetFromFile(data(), link_field_map={}, default_directed=0)

    def test_csv_wkt_and_dataframe(self):
        source=pd.DataFrame({'WKT':[line().wkt],'DIR':['0'],'ID':['001'],'SPD':['12']})
        mapping={'geometry':'WKT','directed':'DIR','link_id':'ID','free_speed':'SPD'}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'links.csv'
            source.to_csv(path,index=False)
            for table in [source,path]:
                net=sg.getNetFromFile(table,link_field_map=mapping,source_crs=26917,speed_unit='mph')
                self.assertEqual(net.links.source_link_id.tolist(),['001','001'])
                self.assertEqual(len(net.nodes),2)
                self.assertAlmostEqual(net.links.free_speed.iloc[0],19.312128)

    def test_csv_nodes_with_xy(self):
        links=pd.DataFrame({'WKT':[line().wkt],'A':['001'],'B':['002']})
        nodes=pd.DataFrame({'N':['002','001'],'X':[300100,300000],'Y':[4400000,4400000],'population':[20,30]})
        net=sg.getNetFromFile(links,link_field_map={'geometry':'WKT','from_node_id':'A','to_node_id':'B'},default_directed=1,source_crs=26917,node_file=nodes,node_field_map={'node_id':'N','x_coord':'X','y_coord':'Y'},node_source_crs=26917)
        self.assertEqual(net.nodes.node_id.tolist(),['001','002'])
        self.assertEqual(net.nodes.population.tolist(),[30,20])
        self.assertTrue(net.nodes.x_coord.between(-180,180).all())

    def test_directed_flags_endpoints_and_geometry(self):
        frame = data([line(0,100),line(100,200),line(200,300).reverse()], DIR=[0,1,1])
        net = convert(frame, link_field_map={'directed':'DIR'})
        self.assertEqual(len(net.nodes), 4)
        self.assertEqual(len(net.links), 4)
        self.assertEqual(list(zip(net.links.from_node_id,net.links.to_node_id)), [(1,2),(2,4),(3,4),(2,1)])
        coords = frame.to_crs(4326).geometry
        self.assertEqual(wkt.loads(net.links.iloc[2].geometry), coords.iloc[2])
        self.assertEqual(wkt.loads(net.links.iloc[-1].geometry), coords.iloc[0].reverse())
        self.assertTrue(net.links.link_id.is_unique)
        self.assertEqual(net.links.directed.tolist(), [1,1,1,1])

    def test_source_codes_are_not_interpreted(self):
        with self.assertRaisesRegex(ValueError, 'directed'):
            convert(data())
        for code in ['B','F','T',-1,2,'unknown']:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, 'directed'):
                convert(data(DIR=[code]), link_field_map={'directed':'DIR'}, default_directed=0)

    def test_null_directed_requires_explicit_fallback(self):
        with self.assertRaises(ValueError):
            convert(data(DIR=[None]), link_field_map={'directed':'DIR'})
        net = convert(data(DIR=[None]), link_field_map={'directed':'DIR'}, default_directed=1)
        self.assertEqual(len(net.links), 1)

    def test_all_explicit_default_flags(self):
        for value, count in [(1,1),(0,2),(True,1),(False,2),('1',1),('0',2)]:
            with self.subTest(value=value):
                net = convert(data(), default_directed=value)
                self.assertEqual(len(net.links), count)

    def test_nearly_closed_loops_are_retained(self):
        loop = LineString([(300000,4400000),(300020,4400000),(300020,4400020),(300000.0001,4400000)])
        net = convert(data([loop]), default_directed=0)
        self.assertEqual(len(net.nodes),1)
        self.assertEqual(len(net.links),2)
        self.assertGreater(net.links.length.iloc[0],60)

    def test_source_attributes_preserved_not_automatically_used(self):
        frame = data(SPEED=[45], LANE=[4], LTS=[2], custom=['kept'])
        original = frame.copy()
        net = convert(frame, mode_types='bike', default_directed=0)
        self.assertEqual(net.links.SPEED.tolist(), [45,45])
        self.assertEqual(net.links.LANE.tolist(), [4,4])
        self.assertEqual(net.links.custom.tolist(), ['kept','kept'])
        self.assertTrue(net.links.free_speed.isna().all())
        self.assertTrue(net.links.lanes.isna().all())
        pd.testing.assert_frame_equal(frame, original)
        sg.fillLinkAttributesWithDefaultValues(net, default_speed=True, default_capacity=True)
        self.assertEqual(net.links.free_speed.tolist(), [19.312128,19.312128])
        self.assertEqual(net.links.capacity.tolist(), [1500,1500])
        self.assertTrue(net.links.lanes.isna().all())

    def test_field_mapping_and_units(self):
        net = convert(data(DIST=[0.1], SPD=[12], LANES=[2], CAP=[700], ID=['001']),
            default_directed=0, link_field_map={'length':'DIST','free_speed':'SPD','lanes':'LANES','capacity':'CAP','link_id':'ID'},
            length_unit='mile', speed_unit='mph', capacity_per_lane=True)
        self.assertAlmostEqual(net.links.length.iloc[0],160.9344)
        self.assertAlmostEqual(net.links.free_speed.iloc[0],19.312128)
        self.assertEqual(net.links.capacity.iloc[0],1400)
        self.assertEqual(net.links.source_link_id.tolist(),['001','001'])

    def test_reverse_specific_attributes(self):
        net = convert(data(AB=[20],BA=[10],L_AB=[2],L_BA=[1]),
            default_directed=0, link_field_map={'free_speed':'AB','lanes':'L_AB'},
            reverse_field_map={'free_speed':'BA','lanes':'L_BA'},speed_unit='mph')
        self.assertEqual(net.links.lanes.tolist(),[2,1])
        self.assertAlmostEqual(net.links.free_speed.iloc[1],16.09344)

    def test_lane_totals(self):
        net=convert(data(LN=[4],CAP=[500]), default_directed=0,
            link_field_map={'lanes':'LN','capacity':'CAP'},lanes_are_total=True,capacity_per_lane=True)
        self.assertEqual(net.links.lanes.tolist(),[2,2])
        self.assertEqual(net.links.capacity.tolist(),[1000,1000])
        with self.assertRaisesRegex(ValueError,'Odd'):
            convert(data(LN=[3]),default_directed=0,link_field_map={'lanes':'LN'},lanes_are_total=True)

    def test_multipart_splits_and_distributes_length(self):
        shape=MultiLineString([line(0,100),line(200,400)])
        net=convert(data([shape],LEN=[600]),default_directed=1,link_field_map={'length':'LEN'})
        self.assertEqual(net.links.source_part.tolist(),[0,1])
        self.assertEqual(net.links.length.tolist(),[200,400])
        self.assertEqual(len(net.nodes),4)

    def test_existing_ids_and_node_file(self):
        frame=data(A=[10],B=[20])
        nodes=gpd.GeoDataFrame({'N':[20,10],'ZONE':[None,9],'label':['end','start']},geometry=[Point(line().coords[-1]),Point(line().coords[0])],crs=26917)
        net=convert(frame,default_directed=0,link_field_map={'from_node_id':'A','to_node_id':'B'},node_file=nodes,node_field_map={'node_id':'N','zone_id':'ZONE'})
        self.assertEqual(net.nodes.node_id.tolist(),[10,20])
        self.assertEqual(net.nodes.label.tolist(),['start','end'])
        self.assertEqual(net.nodes.zone_id.iloc[0],9)
        self.assertEqual(list(zip(net.links.from_node_id,net.links.to_node_id)),[(10,20),(20,10)])

    def test_existing_ids_preserve_grade_separation(self):
        net=convert(data([line(),line()],A=[10,30],B=[20,40]),default_directed=1,link_field_map={'from_node_id':'A','to_node_id':'B'})
        self.assertEqual(len(net.nodes),4)

    def test_inconsistent_existing_ids_fail(self):
        with self.assertRaisesRegex(ValueError,'different endpoints'):
            convert(data([line(),line(0,200)],A=[10,10],B=[20,20]),default_directed=1,link_field_map={'from_node_id':'A','to_node_id':'B'})

    def test_missing_node_reference(self):
        nodes=gpd.GeoDataFrame({'N':[10]},geometry=[Point(line().coords[0])],crs=26917)
        with self.assertRaisesRegex(ValueError,'missing referenced'):
            convert(data(A=[10],B=[20]),default_directed=1,link_field_map={'from_node_id':'A','to_node_id':'B'},node_file=nodes,node_field_map={'node_id':'N'})

    def test_geographic_and_feet_crs(self):
        for crs in [4326,3735]:
            with self.subTest(crs=crs):
                frame=data().to_crs(crs)
                net=convert(frame,default_directed=1,metric_crs=26917)
                self.assertAlmostEqual(net.links.length.iloc[0],100,places=5)
                self.assertTrue(net.nodes.x_coord.between(-180,180).all())
        auto=convert(data().to_crs(4326),default_directed=1)
        self.assertTrue(auto.metric_crs.is_projected)
        self.assertAlmostEqual(auto.links.length.iloc[0],100,delta=1)

    def test_missing_crs(self):
        frame=data().set_crs(None,allow_override=True)
        with self.assertRaisesRegex(ValueError,'CRS'):
            convert(frame,default_directed=1)
        net=convert(frame,source_crs=26917,default_directed=1)
        self.assertEqual(net.links.length.iloc[0],100)

    def test_invalid_geometries(self):
        for geometry in [None,Point(0,0),Polygon([(0,0),(1,0),(1,1),(0,0)]),LineString([(0,0),(0,0)])]:
            with self.subTest(geometry=geometry), self.assertRaises(ValueError):
                convert(data([geometry]),default_directed=1)

    def test_rounding_collapse(self):
        with self.assertRaisesRegex(ValueError,'collapses'):
            convert(data([line(0,0.001)]),default_directed=1)
        self.assertEqual(len(convert(data([line(0,0.001)]),default_directed=1,node_precision=4).links),1)

    def test_crossings_are_not_silently_connected(self):
        cross=LineString([(300050,4399950),(300050,4400050)])
        net=convert(data([line(),cross]),default_directed=1)
        self.assertEqual(len(net.nodes),4)

    def test_raw_name_collisions_and_optout(self):
        frame=data(length=[999],source_length=[888],free_speed=[12])
        net=convert(frame,default_directed=1,link_field_map={'free_speed':'free_speed'},speed_unit='mph')
        self.assertEqual(net.links.length.iloc[0],100)
        self.assertEqual(net.links.source_source_length.iloc[0],999)
        self.assertEqual(net.links.source_length.iloc[0],888)
        self.assertEqual(net.links.source_free_speed.iloc[0],12)
        self.assertAlmostEqual(net.links.free_speed.iloc[0],19.312128)
        clean=convert(frame,default_directed=1,keep_source_columns=False)
        self.assertNotIn('source_length',clean.links)

    def test_defaults_only_fill_missing(self):
        net=convert(data([line(),line(100,200)],SPD=[10,None],TYPE=['path','path']),mode_types='bike',default_directed=1,link_field_map={'free_speed':'SPD','link_type_name':'TYPE'})
        sg.fillLinkAttributesWithDefaultValues(net)
        self.assertTrue(pd.isna(net.links.free_speed.iloc[1]))
        sg.fillLinkAttributesWithDefaultValues(net,default_speed=True,default_speed_dict={'path':15})
        self.assertEqual(net.links.free_speed.tolist(),[10,15])

    def test_bad_parameters(self):
        variants=[{'mode_types':'air'}, {'metric_crs':4326}, {'metric_crs':3735}, {'speed_unit':'furlong'}, {'node_precision':-1}, {'link_field_map':{'free_speed':'absent'}}, {'link_field_map':{'from_node_id':'A'}}, {'reverse_field_map':{'from_node_id':'A'}}, {'capacity_per_lane':True}, {'lanes_are_total':True}]
        for options in variants:
            with self.subTest(options=options),self.assertRaises(ValueError):
                convert(data(A=[1]),default_directed=1,**options)

    def test_invalid_numeric_values(self):
        for value in ['bad',-1,float('inf')]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                convert(data(SPD=[value]),default_directed=1,link_field_map={'free_speed':'SPD'})

    def test_shapefile_gpkg_and_csv_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for suffix in ['shp','gpkg']:
                path=root/('roads.'+suffix)
                data(DIR=[0],LTS=[1]).to_file(path)
                net=convert(path,mode_types='bike',link_field_map={'directed':'DIR'})
                sg.fillLinkAttributesWithDefaultValues(net,default_speed=True,default_capacity=True)
                output=root/suffix/'nested'
                sg.outputNetToCSV(net,output)
                self.assertEqual(len(pd.read_csv(output/'node.csv')),2)
                links=pd.read_csv(output/'link.csv')
                self.assertEqual(len(links),2)
                self.assertTrue(links.from_node_id.isin(net.nodes.node_id).all())


if __name__=='__main__':
    unittest.main()
