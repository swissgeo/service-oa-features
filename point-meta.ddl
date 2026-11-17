-- public."tbl_ogd-local-forecasting-point-meta" definition

-- Drop table

-- DROP TABLE public."tbl_ogd-local-forecasting-point-meta";

CREATE TABLE public."tbl_ogd-local-forecasting-point-meta" (
	id serial4 NOT NULL,
	point_id int4 NOT NULL,
	point_type_id int4 NOT NULL,
	point_name varchar(256) NULL,
	station_abbr varchar(64) NULL,
	postal_code int4 NULL,
	point_type_de varchar(64) NOT NULL,
	point_type_fr varchar(64) NOT NULL,
	point_type_it varchar(64) NOT NULL,
	point_type_en varchar(64) NOT NULL,
	point_height_masl float8 NULL,
	geometry public.geometry(geometry, 2056) NULL,
	CONSTRAINT "pk_tbl_ogd-local-forecasting-point-meta" PRIMARY KEY (id),
	CONSTRAINT "tbl_ogd-local-forecasting-point-meta_id_not_null" NOT NULL id,
	CONSTRAINT "tbl_ogd-local-forecasting-point-meta_point_id_not_null" NOT NULL point_id,
	CONSTRAINT "tbl_ogd-local-forecasting-point-meta_point_type_de_not_null" NOT NULL point_type_de,
	CONSTRAINT "tbl_ogd-local-forecasting-point-meta_point_type_en_not_null" NOT NULL point_type_en,
	CONSTRAINT "tbl_ogd-local-forecasting-point-meta_point_type_fr_not_null" NOT NULL point_type_fr,
	CONSTRAINT "tbl_ogd-local-forecasting-point-meta_point_type_id_not_null" NOT NULL point_type_id,
	CONSTRAINT "tbl_ogd-local-forecasting-point-meta_point_type_it_not_null" NOT NULL point_type_it
);
CREATE INDEX "idx_tbl_ogd-local-forecasting-point-meta_geometry" ON public."tbl_ogd-local-forecasting-point-meta" USING gist (geometry);
